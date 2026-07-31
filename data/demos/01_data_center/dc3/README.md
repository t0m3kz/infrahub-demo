# DC3 - Brexit, Large Scale, Every Vendor Invited

## Overview

**Location:** London 🇬🇧 | **Size:** Large | **Design:** `L` + `ebgp-ebgp` routing

**Fabric Design:** `L` + `ebgp-ebgp` routing — classic 3-tier Clos (leaf → spine → super-spine), pure eBGP
underlay + eBGP overlay, IPv6 P2P links, DC-level border-leaf fronted by firewall/load-balancer.
This is DC1's pattern scaled up to Large — 4 pods instead of 2, 4 super-spines instead of 2 —
except this time Brexit-flavoured London decided one vendor wasn't dramatic enough.

**Philosophy:** "Every pod, a different vendor." Cisco, Arista, Dell, and Edgecore each get their
own pod, all peering happily into the same Dell-flavoured super-spine/border-leaf tier. Because
nothing says "we definitely did not standardize procurement" like four device platforms in one
fabric.

## Architecture

- **Super-Spines:** 4 (Dell PowerSwitch S5232F-ON) — the one thing everyone agrees to peer with
- **Border-Leaf:** 2 (Dell PowerSwitch S5232F-ON), own firewall + load-balancer pair
- **Pods:** 4 | **Spines:** 8 (2 per pod) | **Racks:** 4

| Pod | Vendor    | Spines | Design   | Deployment  |
| --- | --------- | ------ | -------- | ----------- |
| 1   | Cisco     | 2      | L_MIDDLE | middle_rack |
| 2   | Arista    | 2      | L_MIDDLE | middle_rack |
| 3   | Dell      | 2      | L_MIDDLE | middle_rack |
| 4   | Edgecore  | 2      | L_MIDDLE | middle_rack |

## Quick Start

```bash
uv run inv deploy-dc --scenario dc3 --branch your_branch
```

**Warning:** Spine port consumption rates may cause existential dread. Multi-vendor peace of
mind not included.

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
# really quick
uv run inv deploy-dc --scenario dc3 --branch your_branch

# I'm the control nerd
uv run infrahubctl branch create you_branch

# Load topology (this is the point of no return)
uv run infrahubctl object load data/demos/01_data_center/dc3/ --branch you_branch

# Generate fabric (grab coffee, this might take a while)
uv run infrahubctl generator generate_dc name=DC3 --branch you_branch

```

Trigger infrastructure generation in InfraHub UI → Actions → Generator Definitions → generate_dc DC3-Fabric-1

## Fun Fact

The author still uses the mug he bought 25 years ago in London—proof that some British imports last longer than most celebrity marriages, and definitely longer than any network outage.

Unlike certain monarchs, this mug has never abdicated, and it's still on the throne of morning coffee—no royal drama, just reliable caffeine delivery.
