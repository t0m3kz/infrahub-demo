# DC6 - Mixed Vendors: Debug & Dine, Silesian Edition

## Overview

**Location:** Katowice 🇵🇱 | **Size:** Medium | **Platform:** Multi-Vendor (Layer-Mix) | **Design:** `spine-leaf-middlerack-4spine`

Half the cost of Western Europe, double the sarcasm. Multi-vendor at LAYER level—Arista spines with Dell leafs, Edgecore spines with Cisco leafs. If you've ever wanted to watch vendors argue about port-channel naming, this is your chance.

**Motto:** "Why settle for one vendor's bugs when you can have them all?"

## Architecture

- **Super Spines:** 2 (Cisco N9K-C9336C-FX2)
- **Pods:** 2 | **Spines:** 8 (4+4) | **Racks:** 4
- **Deployment:** `middle_rack` (both pods) - Mixed vendors per layer because chaos breeds character

| Pod | Spines | Spine Vendor | Leaf/ToR Vendor | Design                       | Site Layout |
| --- | ------ | ------------ | --------------- | ---------------------------- | ----------- |
| 1   | 4      | Arista       | Dell SONiC      | spine-leaf-middlerack-4spine | small-dc    |
| 2   | 4      | Edgecore     | Cisco NX-OS     | spine-leaf-middlerack-4spine | small-dc    |

## Quick Start

```bash
uv run inv deploy-dc --scenario dc6 --branch your_branch
```

**Warning:** Vendor interop at scale. May cause spontaneous VLAN migrations and philosophical debates.

**Silesian Fact:** The rolada-to-latency ratio here is unbeatable. Also, we have 4 spines per pod because bootstrap data doesn't negotiate

---

## Quick Start

```bash
# really quick
uv run inv deploy-dc --scenario dc6 --branch your_branch

# I'm the control nerd
uv run infrahubctl branch create you_branch

# Load topology (this is the point of no return)
uv run infrahubctl object load data/demos/01_data_center/dc6/ --branch you_branch

# Generate fabric (grab coffee, this might take a while)
uv run infrahubctl generator generate_dc name=DC6 --branch you_branch

```

Trigger infrastructure generation in InfraHub UI → Actions → Generator Definitions → generate_dc DC6-Fabric-1

## Fun Fact

The author of this repo is both Polish and Silesian—which means he can debug BGP while making rolada, modro kapusta, and gumiklyjzy, all before the kettle boils.

If you have no idea what rolada-modro-kapusta-gumiklyjzy is, visit Upper Silesia and prepare to fall in love (and possibly into a food coma).

Warning: After one bite, you may start speaking Silesian and dreaming of working in a coal mine.