# 31_lisp_fabric: A Data Center Without BGP EVPN

One fabric, DC13, whose overlay is signalled by LISP instead of BGP EVPN. The two border-leafs are the LISP
map-servers/map-resolvers, the edge devices are ETRs, and the underlay is OSPF. There is no BGP in the fabric.

The scope is self-contained: load it on top of the standard bootstrap data. It is **not** part of `30_all`
(see [Why It Is Not in 30_all](#why-it-is-not-in-30_all)).

---

## Read This First

- **No lab has run any of it.** Every IOS-XE LISP line the templates render (`templates/configs/common/cisco_xe_lisp.j2`)
  is marked `unverified-no-lab`. The scope proves that the data model, the generators, the check and the renderer
  agree with each other, not that a Catalyst 9000 accepts the result.
- **Turning `overlay_control_plane` to `lisp` on an existing fabric deletes its overlay BGP.** The routing
  generators track everything they create. Once the planner stops planning the EVPN overlay, the next `add_dc` /
  `add_pod` / `add_rack` run no longer produces the overlay `ManagedBGP` processes and peerings, and the tracking
  cleanup deletes them. Switching back to `bgp_evpn` re-creates them with new ids (and, for the number pools, new
  numbers). Do it on a branch, never on a fabric you want to keep as it is.
- **The PROD SVIs of this scope are unprotected.** `cisco_xe` renders no SVI ACL, no PBR and no VRF statics, and
  DC13 has no firewall or load balancer. This is accepted for a demo that is never merged.
- **A host on a routed tenant segment makes a `cisco_xe` leaf refuse to render, so the scope attaches no hosts.** A
  hosted routed tenant segment gets a zero-trust SVI ACL (`get_acls` always emits an implicit-deny ACL), which
  `cisco_xe` cannot render ("refusing incomplete config"). A fabric edge always carries the edge LISP config
  (`router lisp`, the RLOC locator-set, ITR/ETR and `etr map-server ... key` toward every border) with no
  instance-id while it hosts nothing: the leafs and access-leafs render the OSPF underlay plus that edge config, and the
  border-leafs add the map-server, `site` and EID records.

---

## What Is Inside

| Stage | Directory | What it loads | Generators it fires |
| --- | --- | --- | --- |
| 1 design | `00_design/` | Catalyst 9500 border-leaf, Catalyst 9300 leaf and access-leaf device templates, and their fabric elements | none |
| 2 fabric | `01_fabric/` | The Madrid location, the tenant (`LISP1`), and DC13 with two pods and one network rack each | `add_dc` -> `add_pod` -> `add_rack` |
| 3 footprint | `02_footprint/` | The tenant's PROD and NON-PROD footprints in DC13 | `add_customer_deployment_dc` (no firewall: skipped) |
| 4 segments | `03_segments/` | Two VXLAN segments, one per footprint | `add_vxlan_segment` |

The device templates and design elements live here, not in `data/bootstrap/`, because no other scope runs a
`cisco_xe` fabric. The device types (`C9300-48UXM`, `C9500-32C`) come from `data/bootstrap/06_device_types.yml`.

### The Fabric

DC13 is shaped like [DC2](../01_data_center/dc2/README.md) (size `M`: no super-spine tier, a DC-level border-leaf
tier, spines meshed pod to pod) with these differences:

| | DC2 | DC13 |
| --- | --- | --- |
| `routing_strategy` / `underlay_protocol` | `ebgp-ebgp` / `ipv6` | `ospf-ibgp` / `ipv4` |
| `overlay_control_plane` | `bgp_evpn` | `lisp` |
| Border-leafs | 2 x Arista 7050CX3, firewall + load balancer | 2 x Catalyst 9500-32C, no firewall, no load balancer |
| Leafs / access-leafs | Arista | Catalyst 9300-48UXM (`cisco_xe`) |
| Spines | Arista 7050CX3 | Arista 7050CX3 (their OSPF template exists) |
| Pods | 4 | 2 (2 spines, 2 leafs, 2 access-leafs each) |

`CheckLispFabric` (`checks/lisp_fabric.py`) enforces this profile: `ospf-ibgp`, IPv4 underlay, at least two
border-leafs, every leaf / access-leaf / tor / border-leaf on `cisco_xe`, no super-spine / hyper-spine / border-spine,
a gateway on every segment, and the site key once devices exist. `mlag_create` stays `no`: `cisco_xe` refuses MLAG.

Port speeds are approximate datasheet values, chosen so the speed-aware cabling finds a match on every tier: leaf to
spine 100G, access-leaf to leaf 10G, border-leaf to spine 100G.

### The Tenant

`LISP1` has a PROD footprint (`LISP1-P-DC13`) and a NON-PROD one (`LISP1-D-DC13`), each with one VXLAN segment
(`lisp1-web-p`, `lisp1-app-d`). The segment generator allocates the prefix and the anycast gateway from the tenant
pool of the segment's VRF and the VNI from the DC's pool. Each segment becomes an L2 instance-id and, through its
VRF (`PROD` L3 VNI 60000, `NON-PROD` 60001), an L3 instance-id with an `eid-record` on the border-leafs' `site DC13`.

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

`--ref` is the Git branch Infrahub imports generators, transforms and checks from. It must contain the LISP work
(`overlay_control_plane`, `CheckLispFabric`, the IOS-XE LISP renderer).

---

## Loading the Scope

Scope data never goes into `main`. Create a branch and load the four stages **in this order**, waiting after each one
until no generator task is still running:

```bash
uv run infrahubctl branch create lisp-fabric-scenario
export BRANCH=lisp-fabric-scenario
D=data/demos/31_lisp_fabric

uv run infrahubctl object load $D/00_design --branch $BRANCH
uv run infrahubctl object load $D/01_fabric --branch $BRANCH     # longest stage: DC, pods, racks, cabling, OSPF
uv run infrahubctl object load $D/02_footprint --branch $BRANCH
uv run infrahubctl object load $D/03_segments --branch $BRANCH
```

Each stage needs what the previous one's generators create: the footprints name DC13, the segments name the footprints
and draw their VNI from DC13's pool. Load each stage with a single `object load` call; the loader already sorts and
loads files one after another.

### Waiting Between Stages

```bash
uv run infrahubctl task list --state SCHEDULED --state PENDING --state RUNNING
uv run infrahubctl task list --state FAILED --state CRASHED
```

Run the first until the list is empty, then check that the second is too. Fix a failed task before loading the next
stage.

---

## Checking the Result

On the branch, DC13 should have 2 border-leafs, 4 spines, 4 leafs and 4 access-leafs, a `ManagedOSPF` on each, no
`ManagedBGP` anywhere, a `RoutingPassword` named `dc13-lisp-site-key` attached as the DC's `lisp_site_key`, and two active
segment deployments. `CheckLispFabric` is targetless: it runs in a proposed change from the branch and must report nothing. Render a
border-leaf from the proposed change's artifacts, or in-process as `test_70` does.

The border-leaf config contains `router lisp`, the map-server, `site DC13` and the L3 `eid-record`s, and no
`l2vpn evpn`, `nve1` or `router bgp`. A leaf or access-leaf config contains `router lisp` and the
`etr map-server ... key` lines toward both borders (and no map-server role, `site` or instance-id), and no EVPN either.

The integration suite does all of it, including a second `add_dc` run that must change nothing:

```bash
uv run invoke test-integration-lisp
```

---

## Why It Is Not in 30_all

A DC13 in `30_all` would break the expectations pinned to its three DCs (the DC role counts, the all-eBGP
assertions of `test_61`, the firewall-context and transit counts, the ASN grid, the reference-fabric pin), and it
would put a lab-unverified platform into `test_65`, the idempotency run that gates every release. The scope has its
own test, `tests/integration/test_70_lisp_fabric.py`, with its own rerun assertion.
