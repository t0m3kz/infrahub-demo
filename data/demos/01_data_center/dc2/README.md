# DC2 - The "Arista EOS" Edition
*Middle Rack Data Center with Hierarchical Naming Strategy*

## Overview
**Location:** Paris 🇫🇷 (The City of Light - where your packets enjoy croissants and romantic latency)

**Size:** Small (S) - Efficient, elegant, and budget-friendly

**Platform:** Arista EOS - API-driven modern network OS

**Design Pattern:** S-Middle-MR (Small Middle Rack)

**Use Case:** When the CFO says "make it work but don't make me cry" and you actually deliver. DC2 proves you don't need four pods and a mortgage to build reliable infrastructure. Just 2 pods, 4 racks, and a healthy respect for hierarchical aggregation. It's the Parisian café of data centers - small, efficient, and everyone knows everyone.

---

## Architecture (Minimalism with a French Accent)

### Fabric Scale
- **Super Spines:** 2 (Arista DCS-7050CX3-32C-R) - *Your inter-pod highway patrol*
- **Total Pods:** 2 - *Because symmetry is beautiful and troubleshooting is easier*
- **Total Spines:** 4 (2 per pod) - *Just enough aggregation, not too much*
- **Total Racks:** 4 - *Count them: FOUR. Not six. Not three. FOUR.*
- **Deployment Type:** middle_rack (both pods) - *Hierarchical all the way down*

### Pod Structure (The Twin Towers of Efficiency)
| Pod   | Spines | Model                | Racks | Deployment    | Personality                |
|-------|--------|----------------------|-------|--------------|----------------------------|
| Pod 1 | 2      | DCS-7050CX3-32C-R   | 2     | middle_rack  | The Responsible Sibling    |
| Pod 2 | 2      | DCS-7050CX3-32C-R   | 2     | middle_rack  | The Copy-Paste Sibling     |

---

## Hardware Stack (Budget-Conscious Excellence)

### Super Spine Layer
- **Model:** Cisco Nexus N9K-C9336C-FX2
- **Ports:** 36×100GbE - *More than you need, exactly what you want*
- **Role:** Making pods talk to each other without drama
- **Fun Fact:** These switches cost more than your car but last longer

### Spine Layer
- **Model:** Arista DCS-7050CX3-32C-R
- **Ports:** 32x100GbE
- **Role:** Pod-level aggregation
- **Deployment:** Identical in both pods

### Leaf Layer (In Racks)
- **Model:** Arista DCS-7050CX3-32C-R
- **Count:** 2 per rack
- **Role:** Rack-level aggregation
- **Ports:** 36x100GbE

### ToR Layer
- **Model:** Arista DCS-7050CX3-32C-R
- **Count:** 2 per rack
- **Role:** Server connectivity

---

## Deployment Strategy (Middle Rack Mastery)

**ToR Connectivity Pattern:**
```
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
As a teenager, the author visited La Défense and dreamed of working in one of those shiny Parisian towers. Years later, when the chance finally came, he thanked the God that the dream never came true.

Moral: Sometimes your network is better off outside the business district—and so is your sanity.

Bonus: You’ll avoid rush hour, overpriced coffee, and spontaneous meetings about synergy.