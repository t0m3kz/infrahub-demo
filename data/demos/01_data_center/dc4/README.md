# DC4 - Micro-Fabric, No Identity Crisis Needed

## Overview

**Location:** Berlin 🇩🇪 (The hipster capital - your infrastructure is as edgy as the local techno scene)

**Platform:** Edgecore with SONiC - So vendor-neutral, even your hipster barista could deploy it between DJ sets.

**Fabric Design:** `S_BORDER_SPINE` — no super-spine tier, no DC-wide border-leaf. Berlin decided
commitment isn't overrated after all: both pods now run the exact same micro-fabric pattern, each
pod's **border-spine** pair collapsing spine + border-leaf into one device, meshed back-to-back
with the sibling pod, each with its own dedicated firewall/load-balancer pair. eBGP underlay + eBGP
overlay (RFC 7938), IPv6 P2P links — still pure eBGP, because Berlin still doesn't do link-state.

**Use Case:** When the architecture team finally agrees: fewer device tiers, fewer arguments. One
consistent border-spine pattern across both pods instead of a hybrid mixed/ToR identity crisis.

---

## Architecture (One Beat, No Crisis)

### Fabric Scale

- **Border-Spines:** 4 (2+2, Edgecore 7726-32X-O) — collapsed spine + border-leaf, one per pod pair
- **Pods:** 2 | **Racks:** 2 (1 network rack per pod, 8 leafs each)
- **Deployment:** `middle_rack` (both pods), back-to-back mesh between pods (no super-spine tier)

| Pod | Border-Spines | Design | Deployment | Own FW/LB | Personality |
| --- | -------------- | -------------- | ----------- | --------- | ------------ |
| 1 | 2 | S_BORDER_SPINE | middle_rack | yes | Overachiever |
| 2 | 2 | S_BORDER_SPINE | middle_rack | yes | Same Twin |

## Quick Start

```bash
uv run inv deploy-dc --scenario dc4 --branch your_branch
```

**Warning:** May cause identity crisis. Perfect for flexing multi-deployment skills

## Deployment Steps

```bash
# really quick
uv run inv deploy-dc --scenario dc4 --branch your_branch

# I'm the control nerd
uv run infrahubctl branch create you_branch

# Load topology (this is the point of no return)
uv run infrahubctl object load data/demos/01_data_center/dc4/ --branch you_branch

# Generate fabric (grab coffee)
uv run infrahubctl generator generate_dc name=DC4 --branch you_branch

```

Trigger infrastructure generation in InfraHub UI → Actions → Generator Definitions → generate_dc DC4-Fabric-1

## Fun Fact

The author owns a piece of the Berlin Wall—so if your network ever feels divided, just remember: it can be
rebuilt, repurposed, or turned into a conversation starter at tech meetups. It's a daily reminder that even
the toughest partitions eventually fall—sometimes with a little help from automation, sometimes with a
sledgehammer.

Bonus: The author proudly benefits from Germany's Unity Day, enjoying a free holiday every year thanks to
history and a chunk of concrete.

Prost to open borders, open networks, and open source!
