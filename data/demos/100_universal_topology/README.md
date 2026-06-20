# Universal Topology

> *One data model. Every layer. Traceable end-to-end.*

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

---

## Why One Model?

Traditional infrastructure tooling is siloed: DCIM knows about physical devices and cables,
IPAM knows about addresses, a cloud portal knows about VPCs, and a CMDB holds the service
definitions — but none of them talk to each other at the graph level.

When something breaks, tracing an end-to-end path means pivoting across four tools,
cross-referencing hostnames manually, and hoping the data is current.

Infrahub solves this by storing every object — physical, logical, cloud, and virtual —
as nodes and relationships in the **same graph**. That means a single Cypher query can
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

The following traces are possible directly from the graph once this topology is loaded.
All paths are resolved by following relationship edges across the unified model.

### Physical path: server → Equinix edge router

```text
DC1-POD1-SRV-01:eno1
  --[cable]--> DC1-POD1-L1:Ethernet1/49          # server → leaf downlink
  --[cable]--> DC1-POD1-SP1:Ethernet1/11         # leaf uplink → spine
  --[cable]--> DC1-SS1:Ethernet1/1               # spine → super-spine
  --[cable]--> EQX-DC1-PATCH-01:Port1            # super-spine → DC patch panel
  ~~[circuit: PHYS-DC1-FR2-EQX-01]~~             # physical cross-connect to Equinix
  EQX-FR2-PATCH-01:Port1
  --[cable]--> FR2-EDGE-01:Ethernet1/2           # edge patch panel → edge router

Returns 4 paths (2 NICs × 2 spines), ECMP-aware
```

### Office to edge

```text
OFC-STR-BRANCH-EDGE-01:GE1
  --[cable]--> EQX-STR-PATCH-01:Port1
  ~~[circuit: PHYS-OFC-STR-EQX-FR6-01]~~
  EQX-FR6-PATCH-01:Port3
  --[cable]--> FR6-EDGE-02:Ethernet1/1

1 path — branch has one upstream. 3 hops. No DC fabric involved.
```

### Server → cloud (Direct Connect / ExpressRoute)

```text
DC1-POD1-SRV-01:eno1
  → leaf → spine → super-spine               # DC fabric (physical cables)
  → EQX-DC1-PATCH-01 ~~circuit~~ EQX-FR2-PATCH-01  # cross-connect
  → FR2-EDGE-01
  ~~[virtual circuit: CUST1-FR2-AWS-EU-CENTRAL-1-DX]~~
  → CUST1-APP-EU-CENTRAL-1A-01               # EC2 instance in VPC subnet

Same query, different destination → Azure AKS via AM1-EDGE-01 + ExpressRoute:
  → AM1-EDGE-01:VTI-AM1-FR2-PRI ~~VC-EQX-FR2-AM1~~ → AM1-EDGE-01
  ~~[VC: CUST1-AM1-AZURE-WESTEUROPE-ER]~~
  → CUST1-AKS-WESTEUROPE
```

### Server → partner (chained connectors)

```text
DC1-POD1-SRV-01:eno1
  → [DC fabric] → FR2-EDGE-01                # physical cables + cross-connect
  ~~[VC: VC-EQX-FR2-AM1-PRIMARY]~~           # inter-metro virtual circuit
  → AM1-EDGE-01
  ~~[VC: VC-AM1-PARTNER-GLOBEX-IPVPN]~~      # IPVPN to partner
  → EXT-PARTNER-GLOBEX-01

8 paths returned — 2 NICs × 2 spines × 2 Equinix circuits, all resolved automatically
```

### Segment trace with PBR firewall enforcement

```text
Segment: Customer 1 - production - web-frontend  (PBR enabled)

DC1-POD1-SRV-01:bond0
  --[cable]--> DC1-POD1-L1           # server → leaf
  --[cable]--> DC1-CUST1-FW-01       # PBR detour through firewall (leaf-attached)
  --[cable]--> DC1-POD1-SP1          # firewall → spine
  → super-spine → Equinix → cloud

The graph knows: segment carries PBR, segment is attached to DC1-CUST1-FW-01,
FW is leaf-attached on DC1 → detour inserted automatically.
Compare with no-PBR segments: same servers, 2 fewer hops.
```

### Cross-DC server to server

```text
DC1-POD1-SRV-01 → DC1-POD2-SRV-01

Server → Leaf(POD1) → Spine(POD1) → SuperSpine → Spine(POD2) → Leaf(POD2) → Server

Returns 32 paths — the full ECMP matrix across both pods:
2 NICs × 2 leaves × 2 spines × 2 super-spines × 2 spines × 2 leaves × 2 NICs
```

---

## Connector Types Chained Automatically

| Connector | Schema type | Example |
| --- | --- | --- |
| Physical cable | `DcimCable` | Server NIC → leaf downlink |
| Physical circuit | `TopologyPhysicalCircuit` | DC patch panel → Equinix patch panel |
| Virtual circuit | `TopologyVirtualCircuit` | FR2-EDGE-01 VTI → AM1-EDGE-01 VTI |
| Cloud circuit | `TopologyCloudCircuit` | AM1-EDGE-01 → AKS cluster |

Because all four types live in the same graph with consistent relationship names,
a single traversal follows whichever combination is needed without special-casing any layer.

---

## Cluster Capabilities

Beyond connectivity, DC3 and both cloud regions expose full virtualisation capability trees.
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
└── 05_interconnects/  # All cables, circuits, and BGP peering across layers
```

Each subfolder has its own README with further detail.

---

## Load Order

Files load **alphabetically** within each directory. Dependencies are resolved by:

- Prefixing shared prerequisites with `00_` (management IPs and IPAM namespaces
  live in `02_datacenter/00_namespaces.yml` so they load before any DC capability file)
- Placing metro/region definitions in the `01_locations.yml` of the first subfolder
  that needs them (e.g. Stuttgart metro is defined in `str-branch/01_locations.yml`)

---

## Quick Start

```bash
# Create a branch
uv run infrahubctl branch create your_branch

# Load the full topology
uv run infrahubctl object load data/demos/100_universal_topology --branch your_branch
```

Once loaded, the Neo4j graph backing Infrahub can be queried directly to trace
any of the paths described above.

---

*This demo models a real hybrid enterprise topology — not to show how complicated
infrastructure can be, but to show that when it all lives in one model,
you can finally see it whole.*
