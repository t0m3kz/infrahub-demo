# 30_all: The Everything Demo

Every other demo shows one piece. This one loads all of them into one branch (subtle, it is not): three DC fabrics, colocation
cages from three operators, three clouds, a SaaS edge, five offices, sixteen customers' footprints, nine
applications, and the interconnect layer that ties them together.

It is also the only demo that **must be loaded in order**. The steps below are that order. Yes, the order matters; no, it will not forgive you.

---

## Why It Has to Be Staged

Several files point at objects that no file declares. Generators create those objects, and they run
asynchronously after the load that triggers them ("asynchronously" being engineering for "whenever they feel like it"):

- `03_dc/*/05_servers.yml` puts hosts in compute racks. `add_endpoint` then looks for the access-leaf pair
  in the same row, and those leafs only exist once `add_rack` has run.
- `08_interconnects/01_colo_onramp/03_carrier_circuits.yml` lands the dark fibres on border leafs by name
  (`bl-dc101101`). `add_dc` creates them, and `add_circuit` then adds the border leaf's DC to the fibre's
  `locations` (the file lists only the cage end). The DCI session over each fibre is built by
  `add_virtual_circuit` from the `dci` `TopologySessionCircuit` of
  `03_virtual_circuits/01_dci_interconnects.yml`, once the fibre is placed. Each DC has
  two fibres into its metro, one per cage edge (both edges are Multi-Site border gateways, linked by the
  `interlink` port and peered iBGP by `add_colocation_zone`).
- `07_applications` references VMs and customer footprints declared in `06_customer_boarding`.

So `infrahubctl object load data/demos/30_all` in one go only works on an instance whose branch already has
the fabrics. That is why it seems to work on a long-lived dev instance and fails on a fresh one ("works on my machine", now with a root cause). **Load one
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
fail with missing references (silently doing nothing is the generators' favourite hobby):

```bash
uv run invoke register-repo --ref main
```

`--ref` is the Git branch Infrahub imports generators, transforms and checks from. Point it at the branch
whose code you want to run, not the one you merely hope is correct.

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
some time. Go get coffee; then get a second one.

### Loading by Hand

All IPAM objects use the built-in `default` namespace. Routing belongs to the bootstrap `SecurityZone`
(`PROD`, `NONPROD`, `INTERNET`, `MANAGEMENT`), realized per deployment as a `ManagedVRF`, rather than to
an IPAM namespace. Circuits, routed interfaces and standalone cloud/office prefix registrations retain their
explicit routing zones; segment-owned prefixes use their segment's zone.

Load this scenario separately from `00_full_topology`: both author `198.18.0.0/24`, for the Acme partner in
PROD here and for the public blob endpoint in INTERNET there. Neither CIDR is renumbered to make the scenarios coexist.

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
| 4 | interconnects | Colo on-ramp (fibres, the Megaport cross-connect, the virtual connections to the cloud transports, cage cabling), cloud hub, the DCI circuits, the SD-WAN VRF handoff, internet transit, the SD-WAN gateway's own internet access, the partner tunnel | `add_circuit`, `add_virtual_circuit` |

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
from the next (tip: don't be clever).

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
failure in an early stage turns into dozens of misleading reference errors later on, each blaming the wrong file with total confidence. Stage 1 takes by far
the longest, because it generates around 20 devices per DC plus all their cabling, addressing and routing.

---

## Checking the Result

Once stage 4 has settled, the branch should contain at least the following (more is fine; fewer means something went quietly wrong):

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
| `TopologyCarrierCircuit` (`internet`) | 16 |
| `TopologySessionCircuit` (`dci`, `handoff` and `tunnel` types) | 18 |

### Circuits: Carrier and Connection

The interconnect layer separates the carrier from the connection that rides it, because "the cable" and "the BGP session" are two different things no matter how many times they get blamed for each other. A `TopologyCarrierCircuit` is a pure
carrier (dark fibre, cross-connect, virtual connection, internet access) and has no session; a `DcimCable` is the carrier inside one
facility. A session circuit is the connection between topologies on which routing is generated, and its role is its
`circuit_type` (`dci`, `handoff`, `tunnel` or `vpn`), one kind. A circuit has no `owner`: it is derived (the office among its locations). Its carrier is never listed: it is derived from the carrier circuits (or the cable) on its ports, or on the
parent interface of a sub-interface.

| `circuit_type` | Declared in | Session |
| --- | --- | --- |
| `dci` (6, unowned) | `03_virtual_circuits/01_dci_interconnects.yml`, one per dark fibre | Underlay eBGP on a `/31` from the DCI pool plus a loopback-to-loopback EVPN session, a generated per-circuit key, MTU 9214 on both ports |
| `handoff` (2 declared) | `04_sdwan/06_vrf_handoff_04_TopologySessionCircuit.yml` | One eBGP session on a `/31` from `Handoff-<SecurityZone.name>-IPv4` (`data/bootstrap/26_handoff_pools.yml`), both sides from our pool |
| `handoff` to a cloud (1 declared) | `03_virtual_circuits/02_aws_prod_handoff_06_TopologySessionCircuit.yml` | None generated: the far end is a cloud peering interface, so the sub-interface address, the `/31` and the eBGP peering are authored (see [Cloud and SaaS Connections](#cloud-and-saas-connections)) |
| `vpn` (none declared, model only) | | An eBGP session over a tunnel interface pair; no IPsec or tunnel configuration is generated, the transform and the check report it as unrendered |
| `tunnel` (1 declared, owned by the customer, plus one per SD-WAN office generated by `add_sdwan_edge`) | `07_zone_policies/01_partner_tunnel_04_TopologySessionCircuit.yml` | None: the SD-WAN orchestrator owns the overlay routing |

Related routed objects are split into one-kind files with the same basename and an ordered suffix; loading the
directory still applies prefixes, interfaces, circuits and sessions in their original dependency order.

`add_virtual_circuit` builds the sessions of the `dci`, `handoff` and `vpn` session circuits; `add_circuit` only places the
carrier circuits and fans out to it when a carrier changes. The only role that is an output is the peering's
`peering_role` (`dci-underlay`, `dci`, `regular`). Which tenants a fibre carries is a graph traversal from the circuit
(its ports, then the segments, peerings and circuits on them), not a circuit per tenant. Nobody is going to model one circuit per tenant by hand, and nobody should.

### Colocation Kit Is Per Zone

A metro (`FR`, `PA`, `AM`, ...) is only the Multi-Site site: it owns the VNI and ASN pools, no devices (a landlord with paperwork but no building). The kit is
declared on the zone that holds it, `FR2` (edge pair `eg-fr201/02` plus firewall pair `fw-fr201/02`) and `PA4`
(`eg-pa401/02`), and device names carry the zone name. `FR6` and `PA6` are customer cages that declare nothing.
Equinix Amsterdam is one `TopologyVirtualColocation`, `AM-NE` (see
[Equinix Network Edge](#equinix-network-edge-am-ne)). A
`TopologyCustomerColocation` footprint is still named after its metro (`C001-P-FR`) and names no zone: it is served by
every zone of its metro that has a firewall pair, one shared firewall context per zone (only `FR2` has firewalls here,
so `C001-P-FR` and `C005-P-FR` share the context of its HA cluster). A metro whose zones have no firewalls gives its
footprints no context, and no, it will not invent one out of sympathy.

### A Segment Terminated on a Firewall Context

Every other DC segment is routed by the fabric, like a well-behaved network. This one is the rebel. C005's `firewalled-dc10` (`07_applications/c005/00_segments.yml`)
names DC10's shared PROD firewall context in `terminated_on` (`shared-fw-dc10101-fw-dc10102-prod`: the context is
generated when C005-P-DC10 is boarded, in an earlier stage, so the name is the one the generators derive from the
firewall pair). The firewall pair is its default gateway: the gateway (the `.1` the segment generator allocates from
`Tenant-PROD-IPv4`, as for every DC segment) is the pair's floating VIP, the border leaves only bridge the segment (a
VLAN, the L2 VNI, a tagged trunk on the service ports facing the pair, no SVI) and each firewall member renders the
`<uplink trunk>.<vlan>` sub-interface with the VLAN of the border leaf it is cabled to. `test_64c` asserts the three
sides on the loaded branch, so the rebel is at least supervised.

### Equinix Network Edge (AM-NE)

`AM-NE` hosts two `CloudNetworkAppliance` routers, `eg-am-ne01` and `eg-am-ne02` (template `C8000V_EDGE`, Cisco
IOS XE): instances we rent from Equinix, not hardware we rack (nobody had to carry anything up a staircase), so there are no cages, cables or edge-pair
interlink, and their ports are virtual NICs (`DcimVirtualInterface`). The provider order reads the Device Type name
(`C8000V`) and the Software Image `cisco_xe-17.12.01a` (`version`, `package`). It is ordered under the
`CloudAccount` `equinix-network-edge` (`02_colo/equinix/am/02_account.yml`), which sorts before the topology
file that names it. `AM` is a Multi-Site site like the other metros, so its edges also get a VTEP loopback and an
overlay `ManagedBGP` process in the metro's site AS (`evpn_rt_as`).

The Fabric virtual connection `FABRIC-FR2-AMNE01` (`01_colo_onramp/03_carrier_circuits.yml`) joins the Fabric port of the cage FR to the NE
router: it is a carrier of type `virtual_connection`, Equinix runs it inside its own platform and the NE end is the virtual NIC of a
provider-hosted appliance. Its Fabric end is the `TopologyProviderInterface` `et-0/0/0.1301` of `EXT-EQX-FABRIC-FR2`
(`01_colo_onramp/02a_fabric_interfaces.yml`), the provider's port of the metro (a `TopologyProviderConnect`, `02_cloud_provider_edges.yml`):
one interface per connection, with its S-tag and bandwidth. The cable from `eg-fr201` `Ethernet1/1` to `et-0/0/0` of that port
(`01_colo_onramp/05_cage_cabling.yml`) is the physical patch under it.

### Cloud and SaaS Connections

The cloud and SaaS side of a connection is the cloud model (`CloudDirectConnect` / `CloudVPNConnection` transports,
`CloudPeeringInterface`, `CloudGateway` with attachments and route tables), shown on its own in `data/demos/00_full_topology`.
There is no provider-connection object (the cloud providers all invented their own vocabulary for the same wire) and no generator that builds the session circuits from one: a `handoff` or `vpn`
session circuit ends on a cloud peering interface, and its session is written by hand.

A cloud region hosts nothing of ours; the provider side is a transport, an interface holder placed by its `region` and
owned by an account of the network team (`08_interconnects/01_colo_onramp/01_cloud_accounts.yml`), that holds only the
peerings. The ports are the provider's own, a `TopologyProviderConnect` where they physically are. The on-ramp declares
three transports and three provider connects (`02_cloud_provider_edges.yml`):

| Transport | Kind | Port, on the provider connect | Reached by |
| --- | --- | --- | --- |
| `dx-30all-euc1-fr2` | AWS Direct Connect, physical, 10G | `dxcon-fr2` of `EXT-AWS-DX-FR2` (FR2 cage) | the cable `XC-EQXFR2-AWS-EUC1` from `eg-fr201` `Ethernet1/13` |
| `dx-30all-euc1-amne` | AWS hosted Direct Connect, logical, 1G, partner account `equinix-network-edge` | `dxcon-amne` of `EXT-AWS-DX-EUC1` (`eu-central-1`) | `FABRIC-AMNE02-AWS-EUC1` (Network Edge router `eg-am-ne02`) |
| `er-30all-weu` | Azure ExpressRoute circuit, logical | `msee-pri`, `msee-sec` of `EXT-AZURE-ER-WEU` (`westeurope`) | `XC-EQXPA4-AZURE-WEU-PRI` and `-SEC` (PA4 cage, one `redundancy_group`) |

The two AWS legs are two transports: they are different kinds of connection that share no LAG. The dedicated Direct
Connect is a cable inside the FR2 cage, the others are `virtual_connection` carriers that list the cage and place the
region by the `location` of the provider connect (`add_circuit`).

C001's PROD connection to AWS is the one leg authored end to end, so the edge policy can be derived from the cloud model:
`HANDOFF-FR2-AWS-EUC1-PROD` joins `eg-fr201` `Ethernet1/13.1000` (address `10.255.143.254/31`, the last link of
`Handoff-PROD-IPv4`) to the private virtual interface `dxcon-fr2.1000` of `dx-30all-euc1-fr2` (peer `10.255.143.255/31`,
AS64512), which lands on the Direct Connect gateway `dxgw-30all`. From there the hybrid attachment reaches the hub and
`tgwa-vpc-nordix` the VPC `nordix-vpc-prod`. The peering `BGP-EG-FR201-AWS-DX-PROD` is hand-written. The Azure legs and the
hosted AWS leg have a transport and a carrier but no handoff yet. They are the wiring without the conversation.

### Internet Transit

`04_internet` (foundation stage) holds Orange's access PE. The ISPs have no place object: a provider's router has no
`deployment`, its provider is its `owner`. Lumen and Cogent sell transit to the colocation cages, Orange is the access ISP of the
SD-WAN offices (the one that sells you the internet and calls it a partnership). Orange's access PE (`EXT-ORANGE`, template `MX204_ISP_ACCESS`) is the far end of every office's
`INET-<office>` circuit: `add_sdwan_edge` takes a free `customer` port on it per office, so a new office adds no
data row. `08_interconnects/06_internet` connects the cages to the transit ISPs, whose provider edges
(`EXT-LUMEN-FRA1`, `EXT-COGENT-PAR1`, ...) it declares. Each cage edge (`eg-fr201/02`, `eg-pa401/02`) gets one 10G
transit circuit on `Ethernet1/14` and an eBGP session in VRF `INTERNET`. The ISPs send a default route only, which is all an ISP has ever really offered. A
DC reaches the internet through these edges, so a flow trace towards the internet ends on an ISP device. All
four edges are Multi-Site border gateways (two per metro), so the default from either ISP reaches the DCs.

THE INTERNET itself (`INTERNET` and its one interface `bus`) is bootstrap data (`data/bootstrap/03b_internet.yml`), so
rule I3 (`checks/circuit_placement.py`) applies here: every ISP router that serves internet access has an uplink carrier
to `INTERNET:bus`. `08_interconnects/06_internet_uplinks` declares them for the four provider edges (`xe-0/1/0`) and
Orange's access PE (`et-0/0/0`), unowned, with only the ISP listed (`add_circuit` derives INTERNET). The partner's
router `EXT-ACME-EDGE1` needs none: it buys access from Cogent, whose router is attached.

The shared SD-WAN gateway `EQX-FR2-SDWAN-GW1` (template `VCG-VA_GATEWAY`) has two physical ports: `eth0`, role
`uplink`, cabled to the cage router (`Ethernet1/12`) and carrying the PROD handoff sub-interface `eth0.1900`, and
`eth1`, role `customer`, its own internet access and the public port every tunnel ends on (`add_sdwan_edge` takes the
one `customer` port, so a second one would make it fail). `08_interconnects/08_gateway_access` declares that access,
`INET-LUMEN-EQXFR2-GW`: an unowned `internet` circuit from `eth1` to a free `customer` port (`et-0/0/1`) of Lumen's Frankfurt edge `EXT-LUMEN-FRA1`, with
the cage `FR2` as its only listed location (`add_circuit` adds the ISP end). It models the gateway's own internet
connection and is the carrier the placement check wants under every tunnel end on `eth1`. It is a modelling
simplification (a polite phrase, admit it): no addressing or BGP is modelled for this access, the offices' traffic reaches the cage over `eth0`. It
sits in its own folder because `06_internet` pairs every circuit with a transit peering.

C001's partner extranet is a `tunnel` `TopologySessionCircuit`, `C001-PARTNER-ACME-FR2`
(`07_zone_policies/01_partner_tunnel.yml`): `eth1` of the gateway to the partner's stub router `EXT-ACME-EDGE1`, (owned by ACME, no
deployment) reaching the internet over `INET-ACME-COGENT` (a free `customer` port of
`EXT-COGENT-FRA1`).

C001's egress policy (`07_zone_policies/05_security_policy.yml`, segment `c001-nordix-prod-p`) only aims at destinations
that are reachable. The border filtering accepts and announces only prefixes an owned object registers, so each
destination is one of these:

| Rule | Destination | Why it is reachable |
| --- | --- | --- |
| 10, `nordix-prod-to-aws` | `10.10.0.0/16` (PROD) | The block of C001's VPC `nordix-vpc-prod` in its account `nordix-aws-prod` (`06_customer_boarding/c001/cloud`), attached to the hub by `tgwa-vpc-nordix` (associated with and propagating into `rt-hub-shared`, the hybrid attachment's table), so the AWS session accepts it for owner C001: the edge sub-interface and the AWS peering interface (`CloudPeeringInterface`) end one handoff circuit, and the block is read from the gateway chain behind the peering (`transforms/helpers/bgp_cloud.py`), owned by the transport's account owner |
| 20, `nordix-prod-to-partner-acme` | `198.18.0.0/24` (PROD) | Registered by C001 behind the tunnel `C001-PARTNER-ACME-FR2` (`registered_prefixes`, `07_zone_policies/01_partner_tunnel.yml`) |
| 30, `nordix-prod-to-saas-zscaler` | `destination_zone: INTERNET` | The Zscaler PoP is public space: nothing registers it, the rule goes to the internet zone like C006's internet egress |

No prefix object exists only for the policy: the VPC block and the Acme range are declared where they are registered.
The C004 Azure VNet (`10.40.0.0/16`) stays a customer VPC of its own, with no handoff behind it, so nothing aims at it.

The ISP devices are in no group, so no artifact or check targets them. Their configuration is the carrier's
business, and we are happy to keep it that way. Inter-VRF routing is not wired to these edges yet. The DCs reach `INTERNET` through one
`TopologyRoutedExchange` per firewall context (`shared-dc11-fw-prod-PROD-INTERNET`, ...), created when a customer is
boarded: a hop through that context, whose legs are its member sub-interfaces in the tenant VRF and in `INTERNET`
(a /29 transit per context and VRF). The border leaves carry the `INTERNET` return routes but no default route
there; the upstream arrives with the internet change. The colocation cages keep the default-namespace P2P path
until then. `PROD` and `NONPROD` never exchange directly (some marriages are better off arranged this way), and `MANAGEMENT` has no exchange yet.

Generators add more devices, prefixes and segments on top of what the files declare, so read these as
minimums (the generators are generous with objects nobody asked for). To review the whole thing as a change, open a Proposed Change from `all-demo-scenario` into `main`.
Infrahub then runs the checks and generates the device configuration artifacts.

---

## Testing the Load

The integration suite loads the same stages on a throwaway Infrahub (testcontainers). It then also
checks that every expected generator ran (trust, but verify, then verify again), and verifies the application graph, the compute layer, the
interconnects and the colocation zone policies:

```bash
uv run invoke test-integration-all-demo
```

`invoke load-all-demo` and the test suite both read the stage list from `ALL_DEMO_LOAD_STAGES` in
[tasks.py](../../../tasks.py). If you add a file to this demo that depends on generated objects, add it to
the matching stage there and in the table above.
