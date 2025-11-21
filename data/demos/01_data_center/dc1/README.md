# DC1 - Large Hierarchical Enterprise Data Center

## Overview
**Location:** Paris
**Size:** Large (L)
**Platform:** Cisco Nexus 9K
**Design Pattern:** L-Hierarchical-MR (Large Hierarchical with Middle Rack)

**Use Case:** Enterprise data center with **mixed deployment types** demonstrating both middle_rack and ToR connectivity within the same fabric. This showcases flexibility in deployment strategies across different pods.

---

## Architecture

### Fabric Scale
- **Super Spines:** 2 (Cisco N9K-C9336C-FX2)
- **Total Pods:** 4
- **Total Spines:** 12 (3+3+4+2 across pods)
- **Total Racks:** 16
- **Deployment Types:** Mixed (Pods 1-2: middle_rack, Pods 3-4: tor)

### Pod Structure
| Pod | Spines | Deployment Type | Purpose |
|-----|--------|----------------|----------|
| Pod 1 | 3 | middle_rack | Hierarchical aggregation |
| Pod 2 | 3 | middle_rack | Hierarchical aggregation |
| Pod 3 | 4 | tor | Direct ToR-to-Spine |
| Pod 4 | 2 | tor | Direct ToR-to-Spine |

### Design Template Constraints
- maximum_super_spines: 4
- maximum_spines: 4 per pod
- maximum_pods: 4
- maximum_leafs: 24
- maximum_rack_leafs: 8
- maximum_middle_racks: 8
- maximum_tors: 48
- naming_convention: hierarchical

---

## Hardware Stack

### Spine Layer
- **Model:** Cisco N9K-C9364C-GX
- **Ports:** 64x100GbE
- **Role:** Pod-level aggregation

### Super Spine Layer
- **Model:** Cisco N9K-C9336C-FX2
- **Ports:** 36x100GbE
- **Role:** Inter-pod connectivity

### Leaf Layer (in racks)
- **Model:** Cisco N9K-C9336C-FX2
- **Ports:** 36x100GbE
- **Role:** Rack-level aggregation (Pods 1-2)

---

## Deployment Strategy

### Middle Rack Deployment (Pods 1-2)
**ToR Connectivity:**
- ToRs connect to Leafs within racks
- If no local Leafs, connect to external Leafs (least utilized)
- Reduces spine port consumption
- Better for hierarchical aggregation

### ToR Deployment (Pods 3-4)
**ToR Connectivity:**
- ToRs connect directly to Spines
- Simpler, flatter topology
- Lower latency
- Better for east-west traffic

---

## Use Case Analysis

### ✅ **Strengths**
- **Flexibility:** Demonstrates multiple deployment strategies in one DC
- **Scalability:** 4 pods allow for significant growth
- **Enterprise Ready:** Cisco Nexus platform with proven track record
- **Testing Platform:** Ideal for comparing middle_rack vs tor deployment

### 🎯 **Best For**
- Large enterprises needing deployment flexibility
- Organizations testing different fabric strategies
- Multi-tenant environments with varying requirements
- Training and demonstration purposes

### 📊 **Capacity Estimate**
- ~16 racks
- ~100-150 server racks (with ToRs)
- Supports thousands of servers

---

## Quick Start

```bash
# Load topology
uv run infrahubctl object load data/demos/01_data_center/dc1/

# Generate fabric
uv run infrahubctl generator create_dc name=DC1 --branch main
```

---

## Files
- `00_topology.yml` - DC and Pod definitions
- `01_suites.yml` - Data center suites/rooms
- `02_racks.yml` - 16 network racks across 4 suites

---

## Related Scenarios
- **DC2/DC6:** Pure middle_rack deployment (M-Standard-MR)
- **DC3:** Pure ToR deployment (S-Flat-ToR)
- **DC4:** Mixed deployment with variety (L-Standard-Mixed)
- **DC5:** Large middle_rack deployment (L-Standard-MR)
