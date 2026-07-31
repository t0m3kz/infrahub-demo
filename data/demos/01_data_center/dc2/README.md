# DC2 - Croissants & Micro-Fabric Reality

## Overview

**Location:** Paris 🇫🇷 (The City of Light - where your packets enjoy croissants and romantic latency)

**Platform:** Arista EOS - So API-driven, even your croissant can trigger a config change.

**Fabric Design:** `S_BORDER_SPINE` — a micro-fabric with no super-spine tier and no DC-wide
border-leaf: pods mesh their own **border-spines** directly to sibling pods (back-to-back), and
each pod carries its own firewall/load-balancer pair on that same border-spine. IPv6 underlay,
eBGP underlay + eBGP overlay. Paris finally admits it doesn't need a whole extra device tier just
to say "bonjour" to the pod next door.

**Use Case:** When the CFO says "make it work but don't make me cry" and you actually deliver. DC2
proves you don't need a super-spine tier, a DC-level border-leaf, and a mortgage to build reliable
infrastructure. Just 2 pods, 2 border-spines each, and their own dedicated firewall/load-balancer.
It's the Parisian café of data centers - small, efficient, and everyone knows everyone.

---

## Architecture (Minimalism with a French Accent)

### Fabric Scale

- **Border-Spines:** 4 (2+2, Arista DCS-7050CX3-32C-R) — collapsed spine + border-leaf, one per pod pair
- **Pods:** 2 | **Racks:** 2 (1 network rack per pod, 8 leafs each)
- **Deployment:** `middle_rack` (both pods), back-to-back mesh between pods (no super-spine tier)

| Pod | Border-Spines | Design | Deployment | Own FW/LB | Personality |
| --- | -------------- | -------------- | ----------- | --------- | ----------------- |
| 1 | 2 | S_BORDER_SPINE | middle_rack | yes | Responsible Twin |
| 2 | 2 | S_BORDER_SPINE | middle_rack | yes | Copy-Paste Twin |

## Quick Start

```bash
uv run inv deploy-dc --scenario dc2 --branch your_branch
```

**Warning:** May cause spontaneous optimization and French food cravings

### ToR Layer

- **Model:** Arista DCS-7050CX3-32C-R
- **Count:** 2 per rack
- **Role:** Server connectivity

---

## Deployment Strategy (Middle Rack Mastery)

**ToR Connectivity Pattern:**

```bash
ToR → Local Leafs (same rack)
     ↓
   Leaf → Spine
          ↓
        Spine → Super Spine
```

## Deployment Steps

```bash
# really quick
uv run inv deploy-dc --scenario dc2 --branch your_branch

# I'm the control nerd
uv run infrahubctl branch create you_branch

# Load topology (this is the point of no return)
uv run infrahubctl object load data/demos/01_data_center/dc2/ --branch you_branch

# Generate fabric (grab coffee, this might take a while)
uv run infrahubctl generator generate_dc name=DC2 --branch you_branch

```

Trigger infrastructure generation in InfraHub UI → Actions → Generator Definitions → generate_dc DC2-Fabric-1

## Fun Fact

As a teenager, the author visited La Défense and dreamed of working in one of those shiny Parisian towers.

Years later, when the chance finally came, he realized it was a truly ridiculous dream—and thanked the
universe for dodging a lifetime of overpriced coffee, endless PowerPoint meetings, rush hour traffic, and
spontaneous synergy sessions.

Moral: Sometimes your network (and your sanity) are better off far away from the business district.
