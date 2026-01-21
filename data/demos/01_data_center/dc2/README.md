# DC2 - Croissants & 4-Spine Reality

## Overview

**Location:** Paris 🇫🇷 | **Size:** Small | **Platform:** Arista EOS | **Design:** `spine-leaf-middlerack-4spine`

When the CFO says "make it work but don't make me cry" and you actually deliver. 2 pods, 4 racks, hierarchical aggregation—because middle management isn't just for org charts. The Parisian café of data centers: elegant, efficient, surprisingly good at packet forwarding.

**Fun Fact:** Bootstrap data says 4 spines or nothing. We chose 4. Revolutionary, we know.

## Architecture

- **Super Spines:** 2 (Arista DCS-7050CX3-32C-R)
- **Pods:** 2 | **Spines:** 8 (4+4) | **Racks:** 4
- **Deployment:** `middle_rack` (both pods) - Direct ToR was too mainstream

| Pod | Spines | Design                       | Site Layout | Personality          |
| --- | ------ | ---------------------------- | ----------- | -------------------- |
| 1   | 4      | spine-leaf-middlerack-4spine | small-dc    | Responsible Twin     |
| 2   | 4      | spine-leaf-middlerack-4spine | small-dc    | Copy-Paste Twin      |

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

## Quick Start

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

Years later, when the chance finally came, he realized it was a truly ridiculous dream—and thanked the universe for dodging a lifetime of overpriced coffee, endless PowerPoint meetings, rush hour traffic, and spontaneous synergy sessions.

Moral: Sometimes your network (and your sanity) are better off far away from the business district.