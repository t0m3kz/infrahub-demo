# 30_all: The Everything Demo

Every other demo shows one piece. This one loads all of them into one branch: three DC fabrics, colocation
cages from three operators, three clouds, a SaaS edge, five offices, sixteen customers' footprints, nine
applications, and the interconnect layer that ties them together.

It is also the only demo that **must be loaded in order**. The steps below are that order.

---

## Why It Has to Be Staged

Several files point at objects that no file declares. Generators create those objects, and they run
asynchronously after the load that triggers them:

- `03_dc/*/05_servers.yml` puts hosts in compute racks. `add_endpoint` then looks for the access-leaf pair
  in the same row, and those leafs only exist once `add_rack` has run.
- `08_interconnects/01_colo_onramp/03_physical_circuits.yml` lands the dark fibres on border leafs by name
  (`bl-dc101101`). `add_dc` creates them, and `add_circuit` then adds the border leaf's DC to the fibre's
  `locations` (the file lists only the cage end). The DCI session over each fibre is built by
  `add_virtual_circuit` from the `TopologyInterconnectCircuit` of
  `03_virtual_circuits/01_dci_interconnects.yml`, once the fibre is placed. Each DC has
  two fibres into its metro, one per cage edge (both edges are Multi-Site border gateways, linked by the
  `interlink` port and peered iBGP by `add_colocation_zone`).
- `07_applications` references VMs and customer footprints declared in `06_customer_boarding`.

So `infrahubctl object load data/demos/30_all` in one go only works on an instance whose branch already has
the fabrics. That is why it seems to work on a long-lived dev instance and fails on a fresh one. **Load one
stage, wait for its generators to finish, then load the next.**

---

## Prerequisites

Start Infrahub and load the schema, menu and bootstrap data:

```bash
export INFRAHUB_ADDRESS="http://localhost:8000"
export INFRAHUB_API_TOKEN="06438eb2-8019-4776-878c-0941b1f1d1ec"

uv run invoke setup
```

Register the repository and load the event actions. Without these, no generator fires and the later stages
fail with missing references:

```bash
uv run invoke register-repo --ref main
```

`--ref` is the Git branch Infrahub imports generators, transforms and checks from. Point it at the branch
whose code you want to run.

---

## Loading the Demo

Demo data never goes into `main`. One task creates the `all-demo-scenario` branch and loads every stage in
order, waiting for the generators after each one:

```bash
uv run invoke load-all-demo
```

It stops at the first stage whose load fails or whose generators fail, and prints the command to resume.
Fix the cause, then pick up where it stopped:

```bash
uv run invoke load-all-demo --from-stage applications
```

`--branch` loads into another branch instead. Stage 1 alone takes a while, so expect the whole run to take
some time.

### Loading by Hand

The task runs the steps below. Use them if you want to watch or change a single stage. Create a branch first:

```bash
uv run infrahubctl branch create all-demo-scenario
export BRANCH=all-demo-scenario
```

Load the stages **in this order**. After each one, wait until no generator task is still running (see
[Waiting Between Stages](#waiting-between-stages)).

| # | Stage | What it loads | Generators it fires |
| --- | --- | --- | --- |
| 1 | foundation | Customers, cloud, colocation (with the shared SD-WAN gateway and orchestrator), the internet's ISPs (`04_internet`), SaaS, offices, and the three DC sites with their fabrics and controllers | `add_colocation_metro` (×9), `add_colocation_zone`, `add_virtual_colocation` (`AM-NE`), `add_dc` → `add_pod` → `add_rack` (longest stage) |
| 2 | compute_and_customers | Application hosts and the boarding set: DC/colocation/cloud/office footprints (one `01_deployments.yml` per customer for its DC footprints), cage kit, VMs | `add_endpoint`, `add_customer_deployment_*`, `add_sdwan_edge` |
| 3 | applications | Segments, applications, deployment-request catalogue | `add_vxlan_segment`, `add_app_application` |
| 4 | interconnects | Colo on-ramp (fibres, cross-connects, cage cabling), cloud hub, the DCI and AM-NE handoff circuits, the SD-WAN VRF handoff, cloud endpoints (VIF and Megaport handoffs), internet transit, the SD-WAN gateway's own internet access, the partner tunnel | `add_circuit`, `add_virtual_circuit` |

```bash
D=data/demos/30_all

# 1. foundation
uv run infrahubctl object load $D/00_customer $D/01_cloud $D/02_colo \
  $D/03_dc/dc10/00_location.yml $D/03_dc/dc10/01_topology.yml \
  $D/03_dc/dc11/00_location.yml $D/03_dc/dc11/01_controllers_virtual.yml $D/03_dc/dc11/02_topology.yml \
  $D/03_dc/dc12/00_location.yml $D/03_dc/dc12/01_controllers_physical.yml \
  $D/03_dc/dc12/02_controllers_virtual.yml $D/03_dc/dc12/03_topology.yml \
  $D/04_internet $D/04_saas $D/05_office --branch $BRANCH

# 2. compute_and_customers
uv run infrahubctl object load $D/03_dc/dc10/05_servers.yml $D/03_dc/dc11/05_servers.yml \
  $D/03_dc/dc12/05_servers.yml $D/06_customer_boarding --branch $BRANCH

# 3. applications
uv run infrahubctl object load $D/07_applications --branch $BRANCH

# 4. interconnects
uv run infrahubctl object load $D/08_interconnects --branch $BRANCH
```

Load each stage with a single `object load` call. The loader already sorts files and loads them one after
another. Splitting a stage across several calls does not make it faster, and it can hide one file's writes
from the next.

### Waiting Between Stages

A stage is done when no task is pending or running anymore:

```bash
uv run infrahubctl task list --state SCHEDULED --state PENDING --state RUNNING
```

Run it until the list is empty, then check that nothing failed:

```bash
uv run infrahubctl task list --state FAILED --state CRASHED
```

You can also watch the **Tasks** page in the UI. Fix any failed task before you load the next stage: a
failure in an early stage turns into dozens of misleading reference errors later on. Stage 1 takes by far
the longest, because it generates around 20 devices per DC plus all their cabling, addressing and routing.

---

## Checking the Result

Once stage 4 has settled, the branch should contain at least:

| Kind | Count |
| --- | --- |
| `TopologyDataCenter` | 3 |
| `TopologyPod` | 6 |
| `LocationRack` | 25 |
| `TopologyCustomerDC` | 10 |
| `TopologyColocationZone` | 12 |
| `TopologyVirtualColocation` | 1 |
| `TopologyCustomerColocation` | 7 |
| `TopologyCustomerCloud` | 4 |
| `TopologyCustomerOffice` | 5 |
| `ManagedVxlanSegment` | 5 |
| `AppApplication` | 9 |
| `TopologyExternalProviders` | 4 |
| `TopologyPhysicalCircuit` (`internet`) | 11 |
| `TopologyVirtualCircuit` (Tunnel, Interconnect and Handoff kinds) | 17 |

### Circuits: Carrier and Connection

The interconnect layer separates the carrier from the connection that rides it. A `TopologyPhysicalCircuit` is a pure
carrier (dark fibre, cross-connect, internet access) and has no session; a `DcimCable` is the carrier inside one
facility. A virtual circuit is the connection between topologies on which routing is generated, and its role is its
kind. Its carrier is never listed: it is derived from the physical circuits (or the cable) on its ports, or on the
parent interface of a sub-interface.

| Kind | Declared in | Session |
| --- | --- | --- |
| `TopologyInterconnectCircuit` (6, unowned) | `03_virtual_circuits/01_dci_interconnects.yml`, one per dark fibre | Underlay eBGP on a `/31` from the DCI pool plus a loopback-to-loopback EVPN session, a generated per-circuit key, MTU 9216 on both ports |
| `TopologyHandoffCircuit` (5, unowned) | `05_cloud_endpoints/00_vif_handoff.yml`, `00_vif_handoff_am_ne.yml`, `03_megaport_handoff.yml`, `03_virtual_circuits/02_am_ne_handoff.yml`, `04_sdwan/06_vrf_handoff.yml` | One eBGP session on a `/31` from `Handoff-<namespace>-IPv4` (`data/bootstrap/26_handoff_pools.yml`); both sides are addressed from our pool |
| `TopologyTunnelCircuit` (1 declared, owned by the customer, plus one per SD-WAN office generated by `add_sdwan_edge`) | `07_zone_policies/01_partner_tunnel.yml` | None: the SD-WAN orchestrator owns the overlay routing |

`add_virtual_circuit` builds the sessions of the Interconnect and Handoff circuits; `add_circuit` only places the
physical circuits and fans out to it when a carrier changes. The only role that is an output is the peering's
`peering_role` (`dci-underlay`, `dci`, `regular`). Which tenants a fibre carries is a graph traversal from the circuit
(its ports, then the segments, peerings and circuits on them), not a circuit per tenant.

### Colocation Kit Is Per Zone

A metro (`FR`, `PA`, `AM`, ...) is only the Multi-Site site: it owns the VNI and ASN pools, no devices. The kit is
declared on the zone that holds it, `FR2` (edge pair `eg-fr201/02` plus firewall pair `fw-fr201/02`) and `PA4`
(`eg-pa401/02`), and device names carry the zone name. `FR6` and `PA6` are customer cages that declare nothing.
Equinix Amsterdam is one `TopologyVirtualColocation`, `AM-NE` (see
[Equinix Network Edge](#equinix-network-edge-am-ne)). A
`TopologyCustomerColocation` footprint is still named after its metro (`C001-P-FR`); its optional `serving_zone`
(`FR2` for `C001` and `C005`) says which zone's firewall pair hosts its firewall context (the shared context of that
HA cluster). A footprint without a `serving_zone` gets no context.

### Equinix Network Edge (AM-NE)

`AM-NE` hosts two `CloudNetworkAppliance` routers, `eg-am-ne01` and `eg-am-ne02` (template `C8000V_EDGE`, Cisco
IOS XE): instances we rent from Equinix, not hardware we rack, so there are no cages, cables or edge-pair
interlink. It is routed to Terraform (`infrastructure_orchestrator: terraform`) and ordered under the
`CloudAccount` `equinix-network-edge` (`02_colo/equinix/am/02_account.yml`), which sorts before the topology
file that names it. `AM` is a Multi-Site site like the other metros, so its edges also get a VTEP loopback and an
overlay `ManagedBGP` process in the metro's site AS (`evpn_rt_as`).

Two PROD `TopologyHandoffCircuit`s chain the cage into the AWS hub through the two routers; both are unowned, in
`virtual_circuits`, and their sessions are generated (`add_virtual_circuit`: a /31 from `Handoff-PROD-IPv4`, an eBGP
IPv4 session between the two BGP processes; a C8000V cannot be an EVPN peer, so no EVPN):

| Circuit | Ends | Carrier (derived from the ports) |
| --- | --- | --- |
| `HANDOFF-FR2-AMNE-PROD` (VLAN 1200) | `eg-fr201` `Ethernet1/1.1200` to `eg-am-ne01` `Ethernet1/1.1200` | the cable `eg-fr201` `Ethernet1/1` to the Equinix Fabric port stub `EXT-EQX-FABRIC-FR2` (`01_colo_onramp/05_cage_cabling.yml`) |
| `HANDOFF-AMNE-AWS-EUC1-PROD` (VLAN 1100) | `eg-am-ne02` `Ethernet1/1.1100` to `EXT-AWS-DX-FR2` `et-0/0/0.1100`, cloud endpoint `vif-transit-am-euc1` | `XC-EQXFR2-AWS-EUC1`, on the parent port of the AWS router |

The cage to NE hop is an Equinix Fabric virtual connection, not carried over the fibre: Equinix runs it inside its own
platform, so the only physical thing of ours under it is the patch to its Fabric port. The NE sub-interfaces need no
carrier. Nothing of the sessions is authored, no address and no BGP peering: the addresses come from
`Handoff-PROD-IPv4`. The second VIF's Direct Connect, `dx-eqx-am-euc1`, is held by `eg-am-ne02`; it is not on the FR2
hybrid attachment, which has a single endpoint.

### Internet Transit

`04_internet` (foundation stage) declares three ISPs under the `Internet` label, each a
`TopologyExternalProviders`: Lumen and Cogent sell transit to the colocation cages, Orange is the access ISP of the
SD-WAN offices. Orange's access PE (`EXT-ORANGE`, template `MX204_ISP_ACCESS`) is the far end of every office's
`INET-<office>` circuit: `add_sdwan_edge` takes a free `customer` port on it per office, so a new office adds no
data row. `08_interconnects/06_internet` connects the cages to the transit ISPs, whose provider edges
(`EXT-LUMEN-FRA1`, `EXT-COGENT-PAR1`, ...) it declares. Each cage edge (`eg-fr201/02`, `eg-pa401/02`) gets one 10G
transit circuit on `Ethernet1/14` and an eBGP session in VRF `INTERNET`. The ISPs send a default route only. A
DC reaches the internet through these edges, so a flow trace towards the internet ends on an ISP device. All
four edges are Multi-Site border gateways (two per metro), so the default from either ISP reaches the DCs.

The shared SD-WAN gateway `EQX-FR2-SDWAN-GW1` (template `VCG-VA_GATEWAY`) has two physical ports: `eth0`, role
`uplink`, cabled to the cage router (`Ethernet1/12`) and carrying the PROD handoff sub-interface `eth0.1900`, and
`eth1`, role `customer`, its own internet access and the public port every tunnel ends on (`add_sdwan_edge` takes the
one `customer` port, so a second one would make it fail). `08_interconnects/08_gateway_access` declares that access,
`INET-LUMEN-EQXFR2-GW`: an unowned `internet` circuit from `eth1` to a free `customer` port (`et-0/0/1`) of Lumen's Frankfurt edge `EXT-LUMEN-FRA1`, with
the cage `FR2` as its only listed location (`add_circuit` adds the ISP end). It models the gateway's own internet
connection and is the carrier the placement check wants under every tunnel end on `eth1`. It is a modelling
simplification: no addressing or BGP is modelled for this access, the offices' traffic reaches the cage over `eth0`. It
sits in its own folder because `06_internet` pairs every circuit with a transit peering.

C001's partner extranet is a `TopologyTunnelCircuit`, `C001-PARTNER-ACME-FR2`
(`07_zone_policies/01_partner_tunnel.yml`): `eth1` of the gateway to the partner's stub router `EXT-ACME-EDGE1`, deployed to the new
`TopologyExternalProviders` `PARTNER-ACME` and reaching the internet over `INET-ACME-COGENT` (a free `customer` port of
`EXT-COGENT-FRA1`). A Megaport Cloud Router is the cloud-neutral handoff example: `HANDOFF-PA4-MEGAPORT-PROD` (VLAN
1300, `eg-pa401` to the stub `EXT-MEGAPORT-MCR1`, AS133937) rides the cross-connect `XC-EQXPA4-MEGAPORT`. The Azure
cross-connect `XC-EQXPA4-AZURE-WEU` has no handoff: no Azure attachment is modelled, so it stays an idle carrier.

C001's egress policy (`07_zone_policies/05_security_policy.yml`, segment `c001-nordix-prod-p`) only aims at destinations
that are reachable. The border filtering accepts and announces only prefixes an owned object registers, so each
destination is one of these:

| Rule | Destination | Why it is reachable |
| --- | --- | --- |
| 10, `nordix-prod-to-aws` | `10.10.0.0/16` (PROD) | The block of C001's VPC `nordix-vpc-prod` in its account `nordix-aws-prod` (`06_customer_boarding/c001/cloud`), attached to the hub by `tgwa-vpc-nordix` (associated with and propagating into `rt-hub-shared`, the hybrid attachment's table), so the AWS handoff session accepts it for owner C001 |
| 20, `nordix-prod-to-partner-acme` | `198.18.0.0/24` (PROD) | Registered by C001 behind the tunnel `C001-PARTNER-ACME-FR2` (`registered_prefixes`, `07_zone_policies/01_partner_tunnel.yml`) |
| 30, `nordix-prod-to-saas-zscaler` | the `internet` segment | The Zscaler PoP is public space: nothing registers it, the rule goes to the Internet segment like C006's internet egress |

No prefix object exists only for the policy: the VPC block and the Acme range are declared where they are registered.
The C004 Azure VNet (`10.40.0.0/16`) stays a customer VPC of its own, with no handoff behind it, so nothing aims at it.

The ISP devices are in no group, so no artifact or check targets them. Their configuration is the carrier's
business. Inter-VRF routing is not wired to these edges yet. The DCs reach `INTERNET` through one
`TopologyRoutedExchange` per firewall context (`dc11-fw-shared-PROD-INTERNET`, ...), created when a customer is
boarded: a hop through that context, whose legs are its member sub-interfaces in the tenant VRF and in `INTERNET`
(a /29 transit per context and VRF). The border leaves carry the `INTERNET` return routes but no default route
there; the upstream arrives with the internet change. The colocation cages keep the default-namespace P2P path
until then. `PROD` and `NON-PROD` never exchange directly, and `MANAGEMENT` has no exchange yet.

Generators add more devices, prefixes and segments on top of what the files declare, so read these as
minimums. To review the whole thing as a change, open a Proposed Change from `all-demo-scenario` into `main`.
Infrahub then runs the checks and generates the device configuration artifacts.

---

## Testing the Load

The integration suite loads the same stages on a throwaway Infrahub (testcontainers). It then also
checks that every expected generator ran, and verifies the application graph, the compute layer, the
interconnects and the colocation zone policies:

```bash
uv run invoke test-integration-all-demo
```

`invoke load-all-demo` and the test suite both read the stage list from `ALL_DEMO_LOAD_STAGES` in
[tasks.py](../../../tasks.py). If you add a file to this demo that depends on generated objects, add it to
the matching stage there and in the table above.
