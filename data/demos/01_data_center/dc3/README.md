# DC3 - Large Scale, Multi-Vendor Pods

Four vendors, four CLIs, one queue for the kettle.

## Overview

**Location:** London, United Kingdom | **Size:** Large | **Design:** `L` + `ebgp-ebgp` routing

**Fabric Design:** `L` + `ebgp-ebgp` routing — classic 3-tier Clos (leaf → spine → super-spine), pure eBGP
underlay + eBGP overlay, IPv6 P2P links, DC-level border-leaf fronted by firewall/load-balancer.
This is DC1's pattern scaled up to Large — 4 pods instead of 2, 4 super-spines instead of 2 —
with a different vendor in each pod.

**Design Principle:** One vendor per pod. Cisco, Arista, Dell, and Edgecore each get their
own pod, all peering into the same Dell super-spine/border-leaf tier.

## Architecture

- **Super-Spines:** 4 (Dell PowerSwitch S5232F-ON)
- **Border-Leaf:** 2 (Dell PowerSwitch S5232F-ON), own firewall + load-balancer pair
- **Pods:** 4 | **Spines:** 8 (2 per pod) | **Racks:** 4

| Pod | Vendor    | Spines | Design   | Deployment  |
| --- | --------- | ------ | -------- | ----------- |
| 1   | Cisco     | 2      | M_MIDDLE | middle_rack |
| 2   | Arista    | 2      | M_MIDDLE | middle_rack |
| 3   | Dell      | 2      | M_MIDDLE | middle_rack |
| 4   | Edgecore  | 2      | M_MIDDLE | middle_rack |

## Quick Start

```bash
# See Deployment Steps below for the current load and generator commands.
```

---

## Deployment Strategy (Middle Rack, Every Vendor)

```bash
Server → ToR/L2-Leaf → Leaf → Spine → Super-Spine
                                          |
                                    Border-Leaf → Firewall → Load-Balancer (PBR)
```

---

## Alternative Quick Start

```bash
# Quick path
# See the manual commands below, or use the automatic created-object trigger.

# Manual path
uv run infrahubctl branch create you_branch

# Load topology
uv run infrahubctl object load data/demos/01_data_center/dc3/ --branch you_branch

# Generate fabric (this might take a while)
uv run infrahubctl generator add_dc name=DC3 --branch you_branch

```

Alternatively, trigger infrastructure generation in InfraHub UI → Actions → Generator Definitions → add_dc for DC3.

## Validation

Use `uv run invoke test-integration-routing` for automatic routing changes and `uv run invoke test-integration` for the full multi-DC matrix.
