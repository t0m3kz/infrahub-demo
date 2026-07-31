# DC3 - Brexit, Flat Border-Spine, Maximum Speed

## Overview

**Location:** London 🇬🇧 | **Size:** Micro | **Platform:** Dell SONiC | **Design:** `S_BORDER_SPINE`

**Fabric Design:** `S_BORDER_SPINE` — no super-spine tier, no DC-wide border-leaf (flat naming
convention because London hates unnecessary formality anyway). Each pod's **border-spine** pair
collapses the spine and border-leaf role into one device, meshes back-to-back with the sibling
pod's border-spines, and hosts its own firewall/load-balancer pair directly. One fewer device tier
to argue about at the pub.

Brexit happened, but your data stays! Every pod carries its own firewall/load-balancer now — no
more shipping FW/LB traffic through a shared DC-level border-leaf.

**Philosophy:** "I don't want any extra hops" — and for once, DC3 actually delivers on it.

## Architecture

- **Border-Spines:** 4 (2+2, Dell PowerSwitch S5232F-ON) — collapsed spine + border-leaf, one per pod pair
- **Pods:** 2 | **Racks:** 2 (1 network rack per pod, 8 leafs each)
- **Deployment:** `middle_rack` (both pods), back-to-back mesh between pods (no super-spine tier)

| Pod | Border-Spines | Design | Deployment | Own FW/LB | Personality |
| --- | -------------- | -------------- | ----------- | --------- | --------------------- |
| 1 | 2 | S_BORDER_SPINE | middle_rack | yes | Pragmatic Londoner |
| 2 | 2 | S_BORDER_SPINE | middle_rack | yes | Same Pragmatic Twin |

## Quick Start

```bash
uv run inv deploy-dc --scenario dc3 --branch your_branch
```

**Warning:** Spine port consumption rates may cause existential dread. Low latency worth it

---

## Deployment Strategy (Mixed — The British Compromise)

**ToR Connectivity Patterns:**

```bash
# Some racks go direct (the ToR contingent)
Server → ToR → Spine → Super Spine

# Other racks add a leaf layer (the middle-rack contingent)
Server → ToR → Leaf → Spine → Super Spine
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

and follow steps from dc1

## Fun Fact

The author still uses the mug he bought 25 years ago in London—proof that some British imports last longer than most celebrity marriages, and definitely longer than any network outage.

Unlike certain monarchs, this mug has never abdicated, and it’s still on the throne of morning coffee—no royal drama, just reliable caffeine delivery.
