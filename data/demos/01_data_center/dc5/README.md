# DC5 - Large Multi-Vendor Data Center

## Overview
**Location:** New York
**Size:** Large (L)
**Platform:** Multi-Vendor (Cisco, Arista, Dell, Edgecore)
**Design Pattern:** L-Standard-MR (Large Standard with Middle Rack)

**Use Case:** Large, **multi-vendor** data center with middle_rack deployment. Demonstrates vendor diversity and interoperability in a large-scale fabric. Each pod uses different vendor equipment.

---

## Architecture

### Fabric Scale
- **Super Spines:** 2 (Cisco N9K-C9336C-FX2)
- **Total Pods:** 4
- **Total Spines:** 12 (3+3+4+2 across pods)
- **Total Racks:** 15
- **Deployment Type:** middle_rack (all pods)

### Pod Structure - Multi-Vendor
| Pod | Spines | Vendor | Model | Racks | Deployment |
|-----|--------|--------|-------|-------|------------|
| Pod 1 | 3 | Cisco | N9K-C9336C-FX2 | 4 | middle_rack |
| Pod 2 | 3 | Arista | DCS-7050CX3-32C-R | 4 | middle_rack |
| Pod 3 | 4 | Dell | PowerSwitch-S5232F-ON | 4 | middle_rack |
| Pod 4 | 2 | Edgecore | 7726-32X-O | 3 | middle_rack |

### Design Template Constraints
- maximum_super_spines: 4
- maximum_spines: 4 per pod
- maximum_pods: 4
- maximum_leafs: 24
- maximum_rack_leafs: 8
- maximum_middle_racks: 8
- maximum_tors: 48
- naming_convention: standard

---

## Hardware Stack

### Super Spine Layer
- **Model:** Cisco N9K-C9336C-FX2
- **Ports:** 36x100GbE
- **Role:** Inter-pod connectivity

### Spine Layer (Multi-Vendor)
- **Pod 1:** Cisco N9K-C9336C-FX2 (36x100GbE, 3 spines)
- **Pod 2:** Arista DCS-7050CX3-32C-R (32x100GbE, 3 spines)
- **Pod 3:** Dell PowerSwitch S5232F-ON (32x100GbE, 4 spines)
- **Pod 4:** Edgecore 7726-32X-O (32x100GbE, 2 spines)

### Vendor Distribution
- **Cisco:** Super Spines + Pod 1 Spines
- **Arista:** Pod 2 Spines
- **Dell:** Pod 3 Spines
- **Edgecore:** Pod 4 Spines

---

## Deployment Strategy

### Middle Rack (All Pods)
**Consistent Deployment Pattern:**
```
ToR → Local Leafs (within rack)
ToR → External Leafs (if no local, least utilized)
```

**Benefits:**
- Vendor-agnostic deployment strategy
- Consistent operations across vendors
- Reduced spine port requirements
- Rack-level aggregation

---

## Use Case Analysis

### ✅ **Strengths**
- **Vendor Diversity:** 4 major vendors in one fabric
- **Interoperability:** Proves multi-vendor operation
- **Large Scale:** 15 racks, 4 pods
- **Risk Mitigation:** No single vendor dependency
- **Best-of-Breed:** Choose best platform per pod
- **Real-World:** Mimics actual enterprise evolution

### 🎯 **Best For**
- Large enterprises avoiding vendor lock-in
- Organizations with existing multi-vendor infrastructure
- Testing interoperability and standards compliance
- Procurement strategies requiring vendor diversity
- MSPs managing mixed customer environments

### 💡 **Unique Features**
- **Multi-Vendor:** Only DC with 4 different vendors
- **Interoperability Test:** Validates BGP/EVPN across vendors
- **Vendor Comparison:** Side-by-side platform evaluation
- **Migration Path:** Shows gradual vendor transitions

### ⚠️ **Considerations**
- More complex operations (different CLIs, features)
- Requires multi-vendor expertise
- Potential feature set differences
- More complex troubleshooting

### 📊 **Capacity Estimate**
- 15 network racks
- ~30-45 server racks (potential)
- Supports 750-1125 servers

---

## Quick Start

```bash
# Load topology
uv run infrahubctl object load data/demos/01_data_center/dc5/

# Generate fabric
uv run infrahubctl generator create_dc name=DC5 --branch main
```

---

## Vendor Testing

### Interoperability Validation
```bash
# Generate full fabric
uv run infrahubctl generator create_dc name=DC5 --branch main

# Verify:
# - BGP peering between different vendors
# - EVPN route exchange
# - Consistent naming across vendors
# - Feature parity where expected
```

---

## Files
- `00_topology.yml` - Multi-vendor DC and Pod definitions
- `01_suites.yml` - 4 suites (NYC-1 Suite-1/2/3/4)
- `02_racks.yml` - 15 network racks with mixed vendor Leafs

---

## Related Scenarios
- **DC1:** Similar scale, single vendor (Cisco)
- **DC2:** Medium Arista deployment
- **DC3:** Small Dell deployment
- **DC4:** Large Edgecore with mixed deployment
- **DC6:** Medium multi-vendor
