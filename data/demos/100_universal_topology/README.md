# Universal Topology

> *One data model. Every layer. Traceable end-to-end.*
>
> *How the traversal queries work remains a closely guarded secret.
> Bribes are accepted. Interpretive dance has not yet been tried but is not ruled out.*

---

## What Is This?

The Universal Topology demo brings every layer of a modern hybrid enterprise —
datacenters, colocation, cloud, offices, external providers, and interconnects —
under a **single unified data model** in Infrahub.

The goal is not complexity for its own sake. The goal is **graph reachability**:
because all layers share the same schema and live in the same graph database,
you can trace a path from a server NIC to a cloud VM, from a branch office to
a partner gateway, or from a VXLAN segment through its firewalls to its cloud
endpoints — **in a single traversal**, without pivoting between tools.

---

## Topology Scope

| Layer | What | Details |
| --- | --- | --- |
| 🏢 Datacenters | DC1, DC2, DC3 | 3-tier Clos fabric, MLAG, firewalls, virtualisation clusters |
| ☁️ Cloud | AWS eu-central-1, Azure westeurope | EKS, AKS, VPCs, Direct Connect, ExpressRoute |
| 🏗️ Colocation | Equinix AM/FR/PA, Coresite, Megaport | Physical cross-connects, patch panels, edge routers |
| 🏬 Offices | London, Edinburgh, Barcelona, Madrid, Stuttgart | SD-WAN/MPLS uplinks to Equinix |
| 🔌 Interconnects | DC↔Equinix, Equinix↔Cloud, Equinix↔Partners | Cables, physical circuits, virtual circuits |
| 🤝 External | BT, Colt, DE-CIX, ACME, Globex | ISPs and partner networks with BGP peering |
| 📱 Applications | C001 (Nordix), C002, C003 | Business app layer mapped to infrastructure |

---

## Why One Model?

Traditional infrastructure tooling is siloed: DCIM knows about physical devices and cables,
IPAM knows about addresses, a cloud portal knows about VPCs, and a CMDB holds the service
definitions — but none of them talk to each other at the graph level.

When something breaks, tracing an end-to-end path means pivoting across four tools,
cross-referencing hostnames manually, and hoping the data is current.

Infrahub solves this by storing every object — physical, logical, cloud, and virtual —
as nodes and relationships in the **same graph**. That means a single query can
follow a path from a server's NIC, through the leaf–spine–super-spine fabric, across a
physical cross-connect, through a virtual circuit, and into a cloud cluster —
without a single tool switch.

### A note on AI-driven infrastructure

If you want AI to help operate your datacenter — suggest changes, detect anomalies,
predict blast radius, or reason about connectivity — it needs **complete information**.
An AI looking at a partial model will give you partial answers. Confident-sounding,
beautifully formatted, completely wrong partial answers.

The unified model is the prerequisite. You cannot ask an AI to trace a path it cannot see,
fix a dependency it does not know exists, or validate a change across a boundary that
lives in a different tool. Garbage in, hallucination out.

When everything — physical, logical, cloud, and virtual — lives in one model,
the AI finally has something real to work with.

---

## What You Can Trace

The following traces follow real objects loaded by this demo.
All paths are resolved by following relationship edges across the unified model.

### Trace 1 — Physical path: DC1 server → Equinix Frankfurt edge

A packet leaving `DC1-POD1-SRV-01` on its first NIC travels through the full Clos fabric
and crosses a physical circuit to reach the Frankfurt edge router in Equinix FR2.

```text
DC1-POD1-SRV-01 : eno1
  --[CBL-DC1-POD1-SRV01-L1-P49]--> DC1-POD1-L1 : Ethernet1/49     # server NIC → leaf downlink
  --[CBL-DC1-POD1-L1-SP1-P11]---> DC1-POD1-SP1 : Ethernet1/11     # leaf uplink → spine
  --[CBL-DC1-SS1-P1-SP1-P51]----> DC1-SS1 : Ethernet1/1           # spine → super-spine
  --[CBL-DC1-SS1-EQX-DC1-PATCH-P1]--> EQX-DC1-PATCH-01 : Port1   # super-spine → patch panel
  ~~[PHYS-DC1-FR2-EQX-01]~~                                        # physical cross-connect
  EQX-FR2-PATCH-01 : Port1
  --[cable]--> FR2-EDGE-01 : Ethernet1/1                           # patch panel → edge router

Returns 4 paths (2 NICs × 2 spines), ECMP-aware.
```

### Trace 2 — Office to edge: Stuttgart branch → Frankfurt

The Stuttgart branch office exits via a single upstream circuit to Equinix FR6,
handing off to `FR6-EDGE-02`.

```text
OFC-STR-BRANCH-EDGE-01 : GE1
  --[cable]--> EQX-FR6-STR-PATCH-01 : Port1
  ~~[PHYS-OFC-STR-EQX-FR6-01]~~                                   # physical circuit (BT/Colt)
  EQX-FR6-PATCH-01 : Port3
  --[cable]--> FR6-EDGE-02 : Ethernet1/1

1 path — the branch has one upstream. 3 hops. No DC fabric involved.
```

### Trace 3 — Server → AWS (Direct Connect)

`DC1-POD1-SRV-01` reaches the Nordix EKS cluster in `eu-central-1` via a Direct Connect
transit VIF originating at FR2, crossing a virtual circuit into AWS.

```text
DC1-POD1-SRV-01 : eno1
  → leaf → spine → DC1-SS1                                        # DC Clos fabric
  --[CBL-DC1-SS1-EQX-DC1-PATCH-P1]--> EQX-DC1-PATCH-01
  ~~[PHYS-DC1-FR2-EQX-01]~~                                       # cross-connect to Equinix FR2
  --> FR2-EDGE-01 : Ethernet1/1
  --[C001-VIF-TRANSIT-FR2 / sub Ethernet1/2.100]~~
  ~~[VC-CNRD-FR2-AWS-EU-CENTRAL-1-DX (link_type: direct_connect_aws)]~~
  --> CUST1-EKS-EU-CENTRAL-1 (AWS EKS cluster, eu-central-1a/b)

BGP session: BGP-FR2-AWS-DX-EU-CENTRAL-1 (FR2-EDGE-01 ↔ AWS TGW, AS64512)
Circuit ID: C001-DX-FR2-EU-CENTRAL-1
```

### Trace 4 — Server → Azure (ExpressRoute)

Same server, different destination. Azure AKS in `westeurope` is reached via Amsterdam
rather than Frankfurt — a different physical path using the AM1 edge router and
an ExpressRoute virtual circuit.

```text
DC1-POD1-SRV-01 : eno1
  → leaf → spine → DC1-SS1                                        # DC Clos fabric
  ~~[PHYS-DC1-FR2-EQX-01]~~                                       # cross-connect to FR2
  --> FR2-EDGE-01
  ~~[VC-EQX-FR2-AM1-PRIMARY (VTI-FR2-AM1-PRI ↔ VTI-AM1-FR2-PRI)]~~   # inter-metro: FR → AM
  --> AM1-EDGE-01 : Ethernet1/2.200
  ~~[VC-CNRD-AM1-AZURE-WESTEUROPE-ER (link_type: express_route_azure)]~~
  --> CUST1-AKS-WESTEUROPE (Azure AKS cluster, westeurope)

BGP session: BGP-AM1-AZURE-ER-WESTEUROPE (AM1-EDGE-01 ↔ Azure MSEE, AS12076)
Circuit ID: C001-ER-AM1-WESTEUROPE
```

### Trace 5 — Server → partner (chained connectors across metros)

`DC1-POD1-SRV-01` reaches the Globex partner network via DC fabric → FR2 → inter-metro
virtual circuit → AM1 → IPVPN to Globex. The graph follows all four connector types
in one traversal.

```text
DC1-POD1-SRV-01 : eno1
  → [DC Clos fabric: leaf/spine/super-spine] → FR2-EDGE-01        # physical cables
  ~~[VC-EQX-FR2-AM1-PRIMARY]~~                                    # FR2 → AM1 inter-metro VTI
  --> AM1-EDGE-01
  ~~[VC-AM1-PARTNER-GLOBEX-IPVPN (VTI-AM1-GLOBEX ↔ VTI-GLOBEX-AM1)]~~
  --> EXT-PARTNER-GLOBEX-01

8 paths returned — 2 NICs × 2 spines × 2 Equinix circuits, all resolved automatically.
```

### Trace 6 — Segment trace with PBR firewall enforcement (DC1)

In DC1, customer segments use **active-passive PBR**: leaf L1 steers traffic via the
active firewall's inside port before it can exit the fabric. The graph knows the
detour exists because the segment carries `pbr_enabled: true` and the firewall
is registered as a capability on the leaf.

```text
Segment: customer-1-web-frontend-production  (CUST001-PROD, PBR enabled)

DC1-POD1-SRV-01 : bond0
  --[cable]--> DC1-POD1-L1 : Ethernet1/49
  --[PBR detour]--> DC1-CUST1-FW-01 : ge-0/0/0 (inside)          # steered by leaf L1 PBR rule
  <return>  DC1-CUST1-FW-01 : ge-0/0/1 (outside)
  --[cable]--> DC1-POD1-L2
  → spine → super-spine → Equinix → cloud

Policy: DC1-CUST1-EAST-WEST-POLICY (deny default, web→app 80/443, app→db 5432)
Compare: non-PBR segment skips the FW entirely — same servers, 2 fewer hops.
```

### Trace 7 — Application → infrastructure: Nordix trade-portal

The application layer maps `AppComponent` nodes to virtual infrastructure.
`trade-portal` (production) touches VMs in two network segments —
`c001-trade-portal-frontend-p` and `c001-trade-portal-backend-p` —
which resolve through VXLAN segments into the DC fabric.

```text
AppApplication: c001-trade-portal-p  (Nordix Ltd., production)
  └─ AppComponent: trade-portal frontend  → network_segment: c001-trade-portal-frontend-p
  │    capabilities: [C001-WEB-VM-01, C001-WEB-VM-02]             # DcimVirtualDevice
  └─ AppComponent: trade-portal backend   → network_segment: c001-trade-portal-backend-p
       capabilities: [C001-APP-VM-01, C001-APP-VM-02]

ManagedNetworkSegment: c001-trade-portal-frontend-p
  → VXLAN (DC3-K8S-PROD or CUST1-EKS-EU-CENTRAL-1 depending on deployment)
  → AppComponent.capabilities resolves the VM → cluster → physical DC path
```

### Trace 8 — Cross-DC server to server (full ECMP matrix)

```text
DC1-POD1-SRV-01  →  DC1-POD2-SRV-01

Server → Leaf(POD1) → Spine(POD1) → SuperSpine → Spine(POD2) → Leaf(POD2) → Server

Returns 32 paths — the full ECMP matrix across both pods:
2 NICs × 2 leaves × 2 spines × 2 super-spines × 2 spines × 2 leaves × 2 NICs
```

---

## The Secret Traversal

All traces above resolve automatically. You write the start and end node.
The graph does the rest.

How exactly? The traversal logic follows relationship edges across `DcimCable`,
`TopologyPhysicalCircuit`, `TopologyVirtualCircuit`, and cloud endpoint links
using a recursive graph walk with connector-type awareness.

The full implementation details — the specific query structure, the hop logic,
the ECMP path enumeration — remain undisclosed.

> *Convincing the author to reveal the traversal implementation is left as an exercise
> to the reader. Approaches that have been tried: asking nicely (failed), asking
> less nicely (also failed), offering coffee (pending). Interpretive dance has not
> yet been attempted and is not ruled out.*
>
> *What is known: it is a single query. What is not known: everything else.
> This is, frankly, the most accurate description of most network documentation.*

---

## Connector Types Chained Automatically

| Connector | Schema type | Example in this demo |
| --- | --- | --- |
| Physical cable | `DcimCable` | `CBL-DC1-POD1-SRV01-L1-P49` — server NIC → leaf |
| Physical circuit | `TopologyPhysicalCircuit` | `PHYS-DC1-FR2-EQX-01` — DC → Equinix FR2 |
| Virtual circuit | `TopologyVirtualCircuit` | `VC-CNRD-FR2-AWS-EU-CENTRAL-1-DX` — FR2 → AWS |
| Inter-metro VTI | `TopologyVirtualCircuit` | `VC-EQX-FR2-AM1-PRIMARY` — Frankfurt → Amsterdam |

Because all four types live in the same graph with consistent relationship names,
a single traversal follows whichever combination is needed without special-casing any layer.

---

## Cluster Capabilities

DC3 and both cloud regions expose full virtualisation capability trees.
Each cluster carries nodes for CNI, storage, ingress, registry, management plane, and
monitoring — queryable alongside the physical topology:

| Cluster | CNI | Storage | Monitoring |
| --- | --- | --- | --- |
| DC3-K8S-PROD | Cilium 1.16.3 / Geneve / BGP | Ceph/Rook 1.15.4 | Prometheus → Thanos |
| DC3-VSPHERE-PROD | VMware DVS 8.0U3 | vSAN 8.0U3 | Datadog 7.58 |
| DC3-NUTANIX-PROD | AHV Virtual Switch | Nutanix Container | Datadog 7.58 |
| CUST1-EKS-EU-CENTRAL-1 | AWS VPC CNI 1.18.3 | EBS CSI 1.35.0 | Prometheus → AMP |
| CUST1-AKS-WESTEUROPE | Azure CNI 1.5.35 | Azure Disk CSI | Prometheus → Azure Monitor |

---

## Folder Structure

```text
100_universal_topology/
├── 00_cloud/          # AWS eu-central-1 and Azure westeurope
├── 01_colocation/     # Equinix (AM, FR, PA), Coresite, Megaport
├── 02_datacenter/     # DC1, DC2, DC3 + shared IPAM namespaces + management IPs
├── 03_offices/        # London, Edinburgh, Barcelona, Madrid, Stuttgart
├── 04_external/       # ISPs (BT, Colt, DE-CIX) and partners (ACME, Globex)
├── 05_interconnects/  # All cables, circuits, and BGP peering across layers
└── 06_applications/   # Business application layer — Nordix (C001), C002, C003
```

Each subfolder has its own README with further detail.

---

## Load Order

Files load **alphabetically** within each directory. Dependencies are resolved by:

- Prefixing shared prerequisites with `00_` (management IPs and IPAM namespaces
  live in `02_datacenter/00_namespaces.yml` so they load before any DC capability file)
- Placing metro/region definitions in the `01_locations.yml` of the first subfolder
  that needs them (e.g. Stuttgart metro is defined in `str-branch/01_locations.yml`)
- Shared ASNs (AS65000/AS64512/AS12076) live in `05_interconnects/cloud-equinix/00_cloud_asns.yml`
  to resolve load-order dependencies across circuit files

---

## Quick Start

```bash
# Create a branch
uv run infrahubctl branch create your_branch

# Load the full topology (all layers in one shot)
uv run infrahubctl object load data/demos/100_universal_topology --branch your_branch
```

Once loaded, start from any node in the Infrahub graph and follow the relationships outward.
The paths described above are all there, waiting. The traversal that finds them is,
as noted, a secret. A very good secret. Possibly the best secret in network automation.

---

*This demo models a real hybrid enterprise topology — not to show how complicated
infrastructure can be, but to show that when it all lives in one model,
you can finally see it whole.*
