# DC6 - Medium Multi-Vendor Data Center

## Overview
**Location:** Seattle
**Size:** Medium (M)
**Platform:** Multi-Vendor (Dell, Cisco, Arista)
**Design Pattern:** M-Standard-MR (Medium Standard with Middle Rack)

**Use Case:** Medium-sized **multi-vendor** data center with middle_rack deployment. Demonstrates vendor interoperability at smaller scale. Cost-effective multi-vendor approach for medium enterprises.

---

## Architecture

### Fabric Scale
- **Super Spines:** 2 (Dell PowerSwitch S5232F-ON)
- **Total Pods:** 2
- **Total Spines:** 4 (2+2 across pods)
- **Total Racks:** 6
- **Deployment Type:** middle_rack (all pods)

### Pod Structure - Multi-Vendor
| Pod | Spines | Vendor | Model | Racks | Deployment |
|-----|--------|--------|-------|-------|------------|
| Pod 1 | 2 | Cisco | N9K-C9336C-FX2 | 3 | middle_rack |
| Pod 2 | 2 | Arista | DCS-7050PX4-32S-R | 3 | middle_rack |

### Design Template Constraints
- maximum_spines: 2 per pod
- maximum_pods: 2
- maximum_leafs: 16
- maximum_rack_leafs: 6
- maximum_middle_racks: 4
- maximum_tors: 24
- naming_convention: standard

---

## Hardware Stack

### Super Spine Layer
- **Model:** Dell PowerSwitch S5232F-ON
- **Ports:** 32x100GbE
- **Role:** Inter-pod connectivity
- **OS:** SONiC (open source)

### Spine Layer (Multi-Vendor)
- **Pod 1:** Cisco N9K-C9336C-FX2 (36x100GbE, NX-OS, 2 spines)
- **Pod 2:** Arista DCS-7050PX4-32S-R (32x100GbE, EOS, 2 spines)

### Vendor Distribution
- **Dell:** Super Spines (SONiC)
- **Cisco:** Pod 1 Spines (NX-OS)
- **Arista:** Pod 2 Spines (EOS)

---

## Deployment Strategy

### Middle Rack (All Pods)
**Vendor-Agnostic Pattern:**
```
ToR → Local Leafs (within rack)
ToR → External Leafs (if no local, least utilized)
```

**Benefits:**
- Same deployment logic across all vendors
- Consistent operations
- Reduced complexity vs. mixed deployment
- Cost-effective aggregation

---

## Use Case Analysis

### ✅ **Strengths**
- **Tri-Vendor:** Dell, Cisco, Arista in one fabric
- **Medium Scale:** Right-sized for SMB/departmental
- **Open + Proprietary:** SONiC (Dell) + NX-OS + EOS
- **Cost Balanced:** Mix of price points
- **Interoperability:** Validates multi-vendor middle_rack

### 🎯 **Best For**
- Medium enterprises (300-700 servers)
- Organizations transitioning between vendors
- Testing multi-vendor interoperability
- Avoiding single-vendor dependency
- Departmental or regional data centers

### 💡 **Unique Features**
- **Tri-Vendor Compact:** All 3 major vendors in small footprint
- **Open Source Super Spine:** Dell SONiC at core
- **Proprietary Pods:** NX-OS and EOS for feature-rich pods
- **Balanced Scale:** Not too large, not too small

### ⚠️ **Considerations**
- Multi-vendor support requirements
- Different CLI/API per vendor
- Feature parity challenges
- Training on 3 platforms

### 📊 **Capacity Estimate**
- 6 network racks
- ~12-24 server racks (potential)
- Supports 300-600 servers

---

## Quick Start

```bash
# Load topology
uv run infrahubctl object load data/demos/01_data_center/dc6/

# Generate fabric
uv run infrahubctl generator create_dc name=DC6 --branch main
```

---

## Interoperability Testing

### Multi-Vendor Validation
```bash
# Generate full fabric
uv run infrahubctl generator create_dc name=DC6 --branch main

# Test scenarios:
# 1. Dell SONiC ↔ Cisco NX-OS BGP peering
# 2. Dell SONiC ↔ Arista EOS BGP peering
# 3. EVPN route exchange across all vendors
# 4. Consistent VXLAN behavior
```

---

## Files
- `00_topology.yml` - Tri-vendor DC and Pod definitions
- `01_suites.yml` - 2 suites (SEA-1 Room-1/2)
- `02_racks.yml` - 6 network racks with mixed vendor equipment

---

## Related Scenarios
- **DC2:** Similar scale, Arista-only
- **DC5:** Large multi-vendor (4 vendors)
- **Comparison:** DC2 (single-vendor) vs DC6 (multi-vendor) at same scale
