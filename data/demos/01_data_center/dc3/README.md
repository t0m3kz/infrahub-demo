# DC3 - Small Flat ToR Data Center

## Overview
**Location:** London
**Size:** Small (S)
**Platform:** Dell PowerSwitch (SONiC)
**Design Pattern:** S-Flat-ToR (Small Flat Top-of-Rack)

**Use Case:** Small, flat data center with **ToR deployment** where all ToR switches connect directly to Spines. Ideal for low-latency, simple deployments with minimal aggregation layers.

---

## Architecture

### Fabric Scale
- **Super Spines:** 2 (Dell PowerSwitch S5232F-ON)
- **Total Pods:** 2
- **Total Spines:** 4 (2+2 across pods)
- **Total Racks:** 8
- **Deployment Type:** tor (all pods)

### Pod Structure
| Pod | Spines | Racks | Deployment Type |
|-----|--------|-------|----------------|
| Pod 1 | 2 | 4 | tor |
| Pod 2 | 2 | 4 | tor |

### Design Template Constraints
- maximum_spines: 2 per pod
- maximum_pods: 2
- maximum_leafs: 0 (no middle aggregation)
- maximum_rack_leafs: 0
- maximum_middle_racks: 0
- maximum_tors: 16
- naming_convention: flat

---

## Hardware Stack

### Spine Layer
- **Model:** Dell PowerSwitch S5232F-ON
- **Ports:** 32x100GbE
- **Role:** Direct ToR aggregation

### Super Spine Layer
- **Model:** Dell PowerSwitch S5232F-ON
- **Ports:** 32x100GbE
- **Role:** Inter-pod connectivity

### Leaf Layer (in racks)
- **Models:** PowerSwitch S5224F-ON (24x25GbE), PowerSwitch S5248F-ON (48x25GbE)
- **Role:** Server connectivity (no ToR aggregation)

---

## Deployment Strategy

### ToR Deployment (All Pods)
**ToR Connectivity:**
```
ToR → Spine (direct connection)
```

**Benefits:**
- Lowest latency (2-hop spine-leaf)
- Simplest topology
- Easy troubleshooting
- Maximum east-west bandwidth

**Trade-offs:**
- Higher spine port consumption
- Less efficient port utilization
- More cables to manage

---

## Use Case Analysis

### ✅ **Strengths**
- **Low Latency:** Direct ToR-to-Spine for 2-hop fabric
- **Open Source:** Dell SONiC OS (open networking)
- **Simplicity:** Flat topology, easy operations
- **Cost:** Dell hardware competitive pricing

### 🎯 **Best For**
- Small enterprises (100-500 servers)
- Latency-sensitive applications (HPC, trading)
- Organizations adopting open networking
- Simple, flat network requirements

### ⚠️ **Limitations**
- Limited scale (8 racks with Leafs)
- Higher spine port requirements
- Less flexible than hierarchical designs

### 📊 **Capacity Estimate**
- 8 network racks
- ~16-32 server racks (potential)
- Supports 200-500 servers

---

## Quick Start

```bash
# Load topology
uv run infrahubctl object load data/demos/01_data_center/dc3/

# Generate fabric
uv run infrahubctl generator create_dc name=DC3 --branch main
```

---

## Testing Mixed Deployment

DC3 is also used for testing **mixed deployment** scenarios:

```bash
# Update Pod 2 to mixed deployment
uv run infrahubctl object load data/demos/02_pod/dc3_pod2_mixed_deployment.yml

# Regenerate Pod 2 racks
uv run infrahubctl generator create_rack name=dc3-r-2-1 --branch main
# ... repeat for dc3-r-2-2, dc3-r-2-3, dc3-r-2-4
```

See `data/demos/02_pod/README.md` for details.

---

## Files
- `00_topology.yml` - DC and Pod definitions
- `01_suites.yml` - 2 rooms (LON-1 Room-1, LON-1 Room-2)
- `02_racks.yml` - 8 network racks with Dell Leafs

---

## Related Scenarios
- **DC1:** Mixed deployment testing
- **DC2/DC6:** Middle rack deployment
- **DC4:** Large mixed deployment
