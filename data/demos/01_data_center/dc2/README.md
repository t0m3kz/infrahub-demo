# DC2 - Back-to-Back: No Super-Spine, Still Grown-Up

## Overview

**Location:** Paris 🇫🇷 (The City of Light - where your packets enjoy croissants and romantic latency)

**Platform:** Arista EOS - So API-driven, even your croissant can trigger a config change.

**Fabric Design:** `M` + `ebgp-ebgp` routing — no super-spine tier at all: every pod's spines mesh
directly with every other pod's spines (full mesh, not a star through some aggregation
layer above them). But unlike the old micro-fabric border-spine pattern, DC2 keeps a real,
separate **border-leaf** tier at the DC level, fronted by its own firewall/load-balancer pair —
nothing here is collapsed into a single device. eBGP underlay + eBGP overlay, IPv6 P2P links.

**Use Case:** When you want the port/latency savings of skipping a super-spine tier, but
you're not small enough (or don't want) to collapse spine+border-leaf into one device like
the border-spine pattern does. DC2 proves those two decisions — "skip the super-spine tier"
and "keep border-leaf separate" — are independent choices, not a package deal.

---

## Architecture (Flat but Still Organized)

### Fabric Scale

- **Border-Leaf:** 2 (Arista DCS-7050CX3-32C-R), with its own firewall + load-balancer pair
- **Pods:** 4 | **Spines:** 8 (2 per pod) | **Racks:** 4
- **Mesh:** every pod's spines connect directly to every other pod's spines — no super-spine
  tier to route through, no DC-level orchestration required (each pod cables itself to its
  existing lower-index siblings as soon as it comes up)

| Pod | Spines | Design   | Deployment  | Personality          |
| --- | ------ | -------- | ----------- | -------------------- |
| 1   | 2      | S_MIDDLE | middle_rack | First Mover          |
| 2   | 2      | S_MIDDLE | middle_rack | Meshes with Pod 1    |
| 3   | 2      | S_MIDDLE | middle_rack | Meshes with 1 & 2    |
| 4   | 2      | S_MIDDLE | middle_rack | Meshes with 1, 2 & 3 |

### Tier Summary

```text
Leaf → Spine ⇄ Spine (every pod, full mesh — no super-spine tier)
                  |
             Border-Leaf → Firewall → Load-Balancer (independent legs, PBR)
```

## Quick Start

```bash
uv run inv deploy-dc --scenario dc2 --branch your_branch
```

**Warning:** May cause spontaneous optimization and French food cravings

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
