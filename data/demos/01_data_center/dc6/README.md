# DC6 - Mixed Vendors: Silesian Buffet, Vendor Bingo, Debug & Dine

## Overview

**Location:** Katowice 🇵🇱 (Poland's and Silesian industrial powerhouse turned tech hub—where the only thing faster than the fiber is the coffee. Half the cost of Western Europe, double the sarcasm, and the rolada-modro-kapusta-gumiklyjzy-to-latency ratio is unbeatable!)

**Size:** Medium (M) - Cost-effective, interoperable, and a little wild. Big enough to cause trouble, small enough to blame someone else.

**Platform:** Multi-Vendor (Cisco, Arista, Dell SONiC, Edgecore SONiC) — because why settle for one vendor's bugs when you can have them all?

**Design Pattern:** M-Standard (Medium with standard naming convention) — the architectural equivalent of a buffet: a little bit of everything, and you never know what you'll get next.

**Use Case:**
Medium-sized multi-vendor data center with middle_rack deployment. It's the perfect playground for engineers who like living dangerously, managers who love vendor bingo, and auditors who enjoy existential dread. Demonstrates vendor interoperability at a scale just big enough to break things, but small enough to blame the intern. Cost-effective multi-vendor approach for medium enterprises—because why settle for one vendor's support hotline when you can have four? If you've ever wanted to see a Cisco, Arista, Dell, and Edgecore device argue about spanning tree, BGP, and whose logo is the ugliest, this is your chance.

Warning: May cause spontaneous VLAN migrations, philosophical debates about port-channel naming, and a sudden urge to update your resume.

---

## Architecture (Layer-Level Vendor Mix)

### Fabric Scale

- **Super Spines:** 2 (Cisco N9K-C9336C-FX2)
- **Total Pods:** 2
- **Total Spines:** 4 (2+2 across pods)
- **Total Racks:** 4 (2 per pod)
- **Deployment Type:** middle_rack (all pods)

### Pod Structure (Vendor Mix Table)

| Pod   | Spines | Vendor                | Leafs/ToRs Vendor | Racks | Deployment   |
|-------|--------|----------------------|-------------------|-------|--------------|
| Pod 1 | 2      | Arista (DCS-7050CX3-32C-R) | Dell SONiC      | 2     | middle_rack  |
| Pod 2 | 2      | Edgecore (7726-32X-O)      | Cisco NX-OS     | 2     | middle_rack  |

---

## Hardware Stack (Multi-Vendor Mayhem)

### Super Spine Layer

- **Model:** Cisco N9K-C9336C-FX2
- **Ports:** 36x100GbE
- **Role:** Inter-pod connectivity
- **Fun Fact:** The neutral overlords

### Spine Layer (Multi-Vendor)

- **Pod 1:** Arista DCS-7050CX3-32C-R (2 spines)
- **Pod 2:** Edgecore 7726-32X-O (2 spines)
- **Ports:** 32x100GbE each
- **Role:** Pod-level aggregation

### Leaf/ToR Layer

- **Pod 1:** Dell SONiC
- **Pod 2:** Cisco NX-OS
- **Role:** Rack-level aggregation and server connectivity

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