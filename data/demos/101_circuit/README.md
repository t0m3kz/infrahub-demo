# 101 Circuit Demo - End-to-End WAN Connectivity

Complete end-to-end WAN infrastructure demonstration showing connectivity from data centers through colocations to cloud providers and SaaS services.

## Architecture Overview

```
DC1 (Frankfurt) ←→ DC2 (Amsterdam)
     ↓                    ↓
Equinix FR5         Equinix AM3
     ↓                    ↓
     ├→ AWS eu-central-1 (Frankfurt)
     ├→ GCP europe-west3 (Frankfurt) / europe-west1 (Belgium)
     ├→ Azure germanywestcentral / westeurope
     └→ Zscaler (Frankfurt/Amsterdam)
```

## Data Structure

### 00_locations/
- **locations.yml** - Physical building locations for circuit termination
  - AMS-EQX-3: Equinix AM3 Amsterdam
  - FRA-EXQ-5: Equinix FR5 Frankfurt

### 01_infrastructure/

#### dc/
- **dc1_frankfurt.yml** - DC1 data center with border routers
  - dc1-border-01, dc1-border-02 (N9K-C9336C-FX2)
  - Ethernet1/[1-8] interfaces for WAN connectivity

- **dc2_amsterdam.yml** - DC2 data center with border routers
  - dc2-border-01, dc2-border-02 (N9K-C9336C-FX2)
  - Ethernet1/[1-8] interfaces for WAN connectivity

#### colocation/
- **equinix_fra.yml** - Equinix Frankfurt colocation zones
  - FR2: fr2-edge-01 (edge router)
  - FR5: fr5-edge-01 (edge router)

- **equinix_ams.yml** - Equinix Amsterdam colocation zones
  - AM3: am3-edge-01, am3-edge-02 (edge routers)

#### cloud/
- **00_customer1_cloud_topology.yml** - Cloud provider hierarchy
  - AWS: eu-central-1 (Frankfurt) with 3 AZs
  - GCP: europe-west1 (Belgium), europe-west3 (Frankfurt)
  - Azure: westeurope (Amsterdam), germanywestcentral (Frankfurt)

- **01_customer1_cloud_accounts.yml** - Cloud account definitions
- **02_customer1_virtual_networks.yml** - VPCs/VNets
- **03_customer1_vpn_gateways.yml** - Cloud VPN gateway resources

#### saas/
- **zscaler.yml** - Zscaler SaaS infrastructure
  - Frankfurt region: zia-fra-gw-01, zia-fra-gw-02
  - Amsterdam region: zia-ams-gw-01, zia-ams-gw-02

### 02_wan_connectivity/circuits/

#### Layer 1 - Physical Circuits
**01_topology_circuits.yml**
- Dark fiber: DC1 ↔ DC2 (2x 100G circuits)
- Cross-connects: DC1 → FR5, DC2 → AM3 (4x 10G circuits)

**02_managed_circuits.yml**
- Physical circuit interface assignments
- Maps TopologyCircuit to device interfaces via ManagedCircuit

#### Layer 2/3 - Virtual Circuits
**03_virtual_circuits.yml**
- VXLAN overlays: DC1 ↔ DC2 (primary/backup)
- BGP peering: DC → Colocation
- Equinix Fabric: Colocation → Cloud (AWS/GCP/Azure)
- IPSec tunnels: DC → Zscaler SaaS

**04_managed_virtual_circuits.yml**
- Virtual circuit routing instances
- BGP configuration (ASN assignments)
- VXLAN endpoint configuration (VNI assignments)
- IPSec tunnel configuration
- Cloud resource mappings (VGW, Cloud Router, ExpressRoute Gateway)

## Connectivity Layers

### Layer 1: Physical Transport
```
DC1-Frankfurt           DC2-Amsterdam
    |                       |
    | DF-DC1-DC2-001 (100G) |
    |=======================|
    | DF-DC1-DC2-002 (100G) |
    |=======================|
    |                       |
    | XC-DC1-FR5-001 (10G)  | XC-DC2-AM3-001 (10G)
    |                       |
Equinix FR5         Equinix AM3
```

### Layer 2/3: Virtual Connectivity
```
DC1 ←VXLAN→ DC2
 |           |
BGP         BGP
 |           |
FR5         AM3
 |           |
Fabric      Fabric
 |           |
├─ AWS (Direct Connect)
├─ GCP (Interconnect)
├─ Azure (ExpressRoute)
└─ Zscaler (IPSec)
```

### Routing Details

**DC1 (AS65001)**
- VXLAN to DC2 (VNI 10001, 10002)
- BGP peering to Equinix FR5 (AS65100)
- IPSec tunnels to Zscaler Frankfurt

**DC2 (AS65002)**
- VXLAN to DC1 (VNI 10001, 10002)
- BGP peering to Equinix AM3 (AS65200)
- IPSec tunnels to Zscaler Amsterdam

**Equinix FR5 (AS65100)**
- BGP to DC1 (AS65001)
- BGP to AWS Frankfurt (AS64512)
- BGP to GCP Frankfurt (AS64513)
- BGP to Azure Germany West (AS12076)

**Equinix AM3 (AS65200)**
- BGP to DC2 (AS65002)
- BGP to AWS Frankfurt (AS64512)
- BGP to GCP Belgium (AS64513)
- BGP to Azure West Europe (AS12076)

## End-to-End Path Examples

### Example 1: DC1 to AWS Frankfurt
```
DC1 → XC-DC1-FR5-001 (physical) →
Equinix FR5 → EQXFAB-FR5-AWS-FRA (Equinix Fabric) →
AWS Direct Connect → VPC in eu-central-1
```

### Example 2: DC1 to DC2
```
DC1 → DF-DC1-DC2-001 (dark fiber) →
DC2 (VXLAN overlay VNI 10001)
```

### Example 3: DC1 to Zscaler (Internet)
```
DC1 → IPSEC-DC1-ZSCALER-FRA-1 (IPSec tunnel) →
Zscaler Frankfurt ZIA Gateway → Internet
```

### Example 4: DC2 to GCP Belgium
```
DC2 → XC-DC2-AM3-001 (cross-connect) →
Equinix AM3 → EQXFAB-AM3-GCP-AMS (Equinix Fabric) →
GCP Cloud Interconnect → VPC in europe-west1
```

## Cypher Query Examples

### Find all paths between DC1 and AWS
```cypher
MATCH path = (dc:TopologyDataCenter)-[:virtual_links*1..5]-(cloud:TopologyCloudRegion)
WHERE dc.name = "DC1" AND cloud.name = "eu-central-1"
RETURN path
```

### Find physical connectivity from DC1
```cypher
MATCH path = (dc:TopologyDataCenter)-[:physical_circuits]-(circuit:TopologyCircuit)
  -[:a_side|z_side]-(managed:ManagedCircuit)
  -[:interface]-(intf:DcimPhysicalInterface)
WHERE dc.name = "DC1"
RETURN path
```

### Find all paths to Zscaler
```cypher
MATCH path = (dc:TopologyDataCenter)-[:virtual_links]-(vpn:TopologyVirtualCircuit)
  -[:virtual_links]-(saas:TopologySaasRegion)
WHERE vpn.link_type = "vpn_ipsec" AND saas.parent = "Zscaler"
RETURN path
```

## Loading Order

```bash
# 1. Locations
uv run infrahubctl object load data/demos/101_circuit/00_locations/

# 2. Infrastructure (order matters: DC → Colocation → Cloud → SaaS)
uv run infrahubctl object load data/demos/101_circuit/01_infrastructure/dc/
uv run infrahubctl object load data/demos/101_circuit/01_infrastructure/colocation/
uv run infrahubctl object load data/demos/101_circuit/01_infrastructure/cloud/
uv run infrahubctl object load data/demos/101_circuit/01_infrastructure/saas/

# 3. WAN Connectivity (order matters: Physical → Virtual)
uv run infrahubctl object load data/demos/101_circuit/02_wan_connectivity/circuits/01_topology_circuits.yml
uv run infrahubctl object load data/demos/101_circuit/02_wan_connectivity/circuits/02_managed_circuits.yml
uv run infrahubctl object load data/demos/101_circuit/02_wan_connectivity/circuits/03_virtual_circuits.yml
uv run infrahubctl object load data/demos/101_circuit/02_wan_connectivity/circuits/04_managed_virtual_circuits.yml
```

## Redundancy Design

- **Physical Layer**: Dual dark fiber circuits (DF-001, DF-002)
- **Cross-Connects**: Dual paths to each colocation (001, 002)
- **VXLAN**: Primary and backup tunnels
- **BGP**: Dual peering (edge-01, edge-02)
- **Cloud Fabric**: Redundant connections from both colocations
- **IPSec**: Primary and backup tunnels to each Zscaler region

## Bandwidth Allocation

- DC-to-DC: 2x 100Gbps (dark fiber)
- DC-to-Colocation: 4x 10Gbps (cross-connects)
- Colocation-to-Cloud: 6x 10Gbps (Equinix Fabric)
- DC-to-Zscaler: 4x 1Gbps (IPSec tunnels)

## Verification Steps

1. **Physical Layer**: Check all TopologyCircuit objects created
2. **Interface Assignments**: Verify ManagedCircuit → Interface mappings
3. **Virtual Circuits**: Confirm TopologyVirtualCircuit with correct locations
4. **Routing Instances**: Validate ManagedVirtualCircuit BGP/VNI configs
5. **Cypher Queries**: Test path finding between all locations
6. **GraphQL**: Query from each location, verify bidirectional visibility
