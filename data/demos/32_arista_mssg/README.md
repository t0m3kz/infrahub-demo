# 32_arista_mssg: An Arista EOS Reference Fabric With Segment Security (MSS-G)

One fabric, DC14, built only from Arista 7280R3 switches on EOS 4.36.2F: the one platform, device type and release
the `reference-arista-eos` group-policy profile row accepts. A tenant with three VXLAN segments and a hand-written
contract runs on it, and the leaf, access-leaf and border-leaf configs carry EOS **Segment Security (MSS-G,
`router segment-security`)**: one segment per gateway prefix, a policy per source under each destination segment,
deny by default.

The scope is self-contained: load it on top of the standard bootstrap data. It is **not** part of `30_all` and is
never merged to main (see [Why It Is Not in 30_all](#why-it-is-not-in-30_all)).

---

## Read This First

- **No lab has run any of it.** The scope and `tests/integration/test_71_arista_mssg.py` prove that the data model,
  the generators, the group tags, the contract builder and the EOS renderer agree with each other: *generators ->
  group tags and rules -> transform -> rendered text*. They do **not** prove that EOS accepts, or enforces, a single
  rendered line.
- **Custom policies, field-sets, application traffic recognition and the fallback policy are AVD-documented only.**
  The EOS 4.36.2F manual lists the built-in `policy-forward-all` and `policy-drop-all` and the feature switch, nothing
  else. Release 4.36.2F may reject the rest, and a whole-config push then loses every line of the leaf. That is why
  the DC loads **staged**: `group_policy_enforce` is left out (false), the section ends in `shutdown`, and
  [the enforce step](#what-the-test-proves) flips it on a branch to show it changes one line.
- **The 7280R3 port plans are approximate.** No datasheet was read for the templates in `00_design/`. They
  assume 48 x 25G plus 8 x 100G (`DCS-7280SR3-48YC8`) and 32 x 100G (`DCS-7280CR3-32P4`, whose real 400G ports are not
  modelled) with the interface names of the 7050SX3 / 7050CX3 templates. They give the cabling generators exactly what
  they need (a speed match on every tier pair, one uplink per spine, `mlag-peer` ports) and nothing else.
- **The release dates of `arista_eos-4.36.2F` are placeholders.** Nothing reads them; the profile gate compares only
  the version.
- **A stored proposed-change artifact is not compared with the in-process render.** The proposed-change helpers report
  artifact status, not content, so the test renders in-process like `test_64`.

---

## What Is Inside

| Stage | Directory | What it loads | Generators it fires |
| --- | --- | --- | --- |
| 1 design | `00_design/` | The `arista_eos-4.36.2F` software image, border-leaf / leaf / access-leaf templates for the two 7280R3 types, their design elements | none |
| 2 fabric | `01_fabric/` | The Rome location, the tenant (`MSSG1`), DC14 with two pods and four racks | `add_dc` -> `add_pod` -> `add_rack` |
| 3 compute | `02_compute/` | Two application servers, one per row | `add_endpoint` |
| 4 footprint | `03_footprint/` | The tenant's PROD footprint in DC14 | `add_customer_deployment_dc` |
| 5 segments | `04_segments/` | `mssg1-web-p`, `mssg1-app-p`, `mssg1-db-p` | `add_vxlan_segment` |
| 6 policies | `05_policies/` | One hand-written egress policy per segment | none |
| 7 application | `06_application/` | One application with a component per segment, instanced on the servers | `add_app_application`, `add_app_component_segment` |

The device templates and design elements live here, not in `data/bootstrap/`, because no other scope runs a 7280R3
fabric. The device types come from `data/bootstrap/06_device_types.yml`; the software image is declared here too.

### The Fabric

DC14 is shaped like [DC10](../30_all/03_dc/dc10/01_topology.yml) (the NX-OS reference fabric), at size `M` with two
`S_MIDDLE` pods and a DC-level border-leaf pair:

| | |
| --- | --- |
| Size / routing | `M`, `ebgp-ebgp`, IPv4 underlay (the schema default is IPv6) |
| Pod 1 | 2 spines (7050CX3, EOS 4.32.1F), `mlag_create: back-to-back` (never `virtual`: `arista_eos_mlag.j2` does not render a virtual peer link) |
| Row 1 | a network rack with a leaf pair **and** an access-leaf pair, a compute rack with `dc14-pod1-server-1` |
| Row 2 | a network rack with a leaf pair, a compute rack with `dc14-pod1-server-2` |
| Pod 2 | 2 spines and no racks; it exists because an `S_MIDDLE` pod takes one border-leaf, so two border-leafs need two pods |
| DC level | 2 x `DCS-7280CR3-32P4` border-leaf, 2 x `CP-26000_FIREWALL`, no load balancer |
| Enforcement | `group_policy_enforce` left out (false): the staged output |

Every VTEP (leaf, access-leaf, border-leaf) is a 7280R3 on EOS 4.36.2F, the profile row's `min_release`. **One VTEP outside the row silently removes the MSS-G block from every device of the DC**, with no
error: `CheckReferenceFabric` reports `DC14 is not a reference fabric: <reason>` as an INFO line, which is why the
test reads its errors first and then its verdict. Spines are not VTEPs and are not checked.

### Why There Are Servers and an Application

A VTEP renders `router segment-security` only for the tenant VRFs it carries, and a leaf or access-leaf carries a VRF
only through a segment tagged on **its own ports** (`transforms/common.py`, `_collect_activations_from_interfaces`). A
segment is tagged on the ports its component's instance is cabled to (`add_app_component_segment`). So without hosts
no leaf renders anything, however many segments the DC has. A border-leaf carries the VRF through the firewall legs of
the tenant's context instead, and needs no host.

`add_endpoint` cables a server to the access-leaf pair of its row when the row's network rack has one, else to the
leaf pair. That is why row 1 has the access-leafs and row 2 does not: the PROD VRF lands on the access-leafs through
`server-1` and on the leafs of row 2 through `server-2`. The leaf pair of row 1 hosts nothing and renders **no**
section, which the test asserts as a negative control. The application declares no `AppDependency`, so it writes no
rule: the contract is the policies of `05_policies/`.

### The Tenant and the Contract

`MSSG1` has one PROD footprint (`MSSG1-P-DC14`) and three VXLAN segments. The segment generator allocates each prefix
and anycast gateway from the PROD tenant pool; the test reads them from the stack instead of pinning an address.

| Rule | Policy (source) | What the EOS leaf renders |
| --- | --- | --- |
| web -> app, tcp 443 and tcp 8000-8010 | `seg-mssg1-web-p-egress` | a custom policy with a `field-set l4-port` (`443, 8000-8010`) under app, and its mirrored reply under web (MSS-G is stateless) |
| web -> db, icmp | `seg-mssg1-web-p-egress` | a port-less `icmp` application under db |
| app -> db, tcp 5432 | `seg-mssg1-app-p-egress` | a custom policy under db |
| db -> app, any | `seg-mssg1-db-p-egress` | `policy-forward-all` under app, and under db a comment `! not expressible (left closed)`: the reply of a permit-all flow cannot be matched by source port |

Every flow between two segments also yields a row to or from the firewall legs (`UNCLASSIFIED`), which the renderer
expresses as the destination segment's `fallback policy`.

---

## Prerequisites

Start Infrahub, load the schema, menu and bootstrap data, and register the repository and event actions, as for
[30_all](../30_all/README.md#prerequisites):

```bash
export INFRAHUB_ADDRESS="http://localhost:8000"
export INFRAHUB_API_TOKEN="06438eb2-8019-4776-878c-0941b1f1d1ec"

uv run invoke setup
uv run invoke register-repo --ref main
```

`--ref` is the Git branch Infrahub imports generators, transforms and checks from. It must contain the Arista MSS-G
renderer (`templates/configs/common/arista_eos_segment_security.j2`).

---

## Loading the Scope

Scope data never goes into `main`. Create a branch and load the seven stages **in this order**, waiting after each one
until no generator task is still running:

```bash
uv run infrahubctl branch create arista-mssg-scenario
export BRANCH=arista-mssg-scenario
D=data/demos/32_arista_mssg

uv run infrahubctl object load $D/00_design --branch $BRANCH
uv run infrahubctl object load $D/01_fabric --branch $BRANCH       # longest stage: DC, pods, racks, cabling, BGP
uv run infrahubctl object load $D/02_compute --branch $BRANCH
uv run infrahubctl object load $D/03_footprint --branch $BRANCH
uv run infrahubctl object load $D/04_segments --branch $BRANCH
uv run infrahubctl object load $D/05_policies --branch $BRANCH
uv run infrahubctl object load $D/06_application --branch $BRANCH
```

Each stage needs what the previous one's generators create: the servers need the switch ports, the footprint names
DC14, the segments name the footprint and draw their VNI from DC14's pool, the policies name the segments, the
application names the segments and the cabled servers. Load each stage with a single `object load` call.

### Waiting Between Stages

```bash
uv run infrahubctl task list --state SCHEDULED --state PENDING --state RUNNING
uv run infrahubctl task list --state FAILED --state CRASHED
```

Run the first until the list is empty, then check that the second is too. Fix a failed task before loading the next
stage.

---

## Checking the Result

On the branch, DC14 should have 2 border-leafs, 4 spines, 4 leafs and 2 access-leafs, three back-to-back MLAG
domains, and the three segments active on it with a gateway and a group tag each. Run `CheckReferenceFabric` and
`CheckSegmentSecurity` from a proposed change, or render a leaf in-process as `test_71` does. A carrying leaf's config
contains, in this order, the `match-list`s (one per segment, the live gateway prefix), `application traffic
recognition` with its `field-set l4-port`s, and `router segment-security` ending in `shutdown`.

### What the Test Proves

```bash
uv run invoke test-integration-arista-mssg
```

| Step | Proves |
| --- | --- |
| Fabric | the roles; three MLAG pairs, none virtual |
| Tenant | the servers on the intended tier; gateway, group tag and deployment per segment; the policies hold exactly the hand-written rules |
| Checks | `CheckReferenceFabric` says `DC14 is a reference fabric (segment_security, 8 VTEPs)` and no error; `CheckSegmentSecurity` no error |
| Matrix | a staged `segment_security` reference site, prefixes equal to the live gateways, the closed any-reply in `unsupported` |
| Renders | leaf, access-leaf and border-leaf: live match-lists and VRF, the `from` / `fallback policy` lines, the field-sets indented inside `application traffic recognition`, the staged comment, a final `shutdown`; a spine and the hostless leaf render none |
| Enforce | `group_policy_enforce` starts no generator; each render differs by the staged comment and `shutdown` -> `no shutdown` |
| Gate | one access-leaf on EOS 4.32.1F removes the block from the whole DC; restoring the image brings it back |
| Rerun | a second `add_vxlan_segment` run changes no tag, prefix, id or render |

The assertions on the render text are exercised offline, against the real `Leaf` transform fed this scope's policies,
by `tests/unit/test_arista_mssg_scope.py`.

---

## Why It Is Not in 30_all

A DC14 in `30_all` would break the expectations pinned to its three DCs (the DC role counts, the firewall-context and
transit counts, the ASN grid, the reference-fabric pin), and it would put a lab-unverified renderer into `test_65`,
the idempotency run that gates every release. The scope has its own test, `tests/integration/test_71_arista_mssg.py`,
with its own rerun assertion, and it is loaded onto a branch that is never merged.
