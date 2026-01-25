# DC4 - Mixed Deployment: Maximum Chaos, Minimum Rules

## Overview

**Location:** Berlin 🇩🇪 (The hipster capital - your infrastructure is as edgy as the local techno scene)

**Size:** Small (S) - Flexible, creative, and a little chaotic

**Platform:** Edgecore with SONiC - So vendor-neutral, even your hipster barista could deploy it between DJ sets.

**Design Pattern:** L-Standard (Large with standard naming convention)

**Use Case:** When the architecture team can't agree on mixed vs flat ToR and someone says "why not both?" Pod 1 goes full mixed deployment, Pod 2 goes pure flat ToR. It's like having a hybrid car that's also a motorcycle. Confusing? Yes. Flexible? Absolutely.

---

## Architecture (Identity Crisis with a Beat)

### Fabric Scale

- **Super Spines:** 2 (Edgecore 7726-32X-O)
- **Total Pods:** 2
- **Total Spines:** 5 (3 in Pod 1, 2 in Pod 2)
- **Total Racks:** 5
- **Deployment Types:** mixed (Pod 1), tor (Pod 2)

### Pod Structure (Split Personality Disorder)

| Pod   | Spines | Racks | Deployment | Personality                |
|-------|--------|-------|------------|----------------------------|
| Pod 1 | 3      | 2     | mixed      | The Sophisticated Engineer |
| Pod 2 | 2      | 3     | tor        | The Pragmatic Minimalist   |

---

## Hardware Stack (Edgecore All the Things)

### Super Spine Layer

- **Model:** Edgecore 7726-32X-O
- **Ports:** 32x100GbE
- **Role:** Inter-pod negotiators
- **SONiC OS:** Open networking for the brave

### Spine Layer

- **Pod 1:** Edgecore 7726-32X-O × 3 spines
- **Pod 2:** Edgecore 7726-32X-O × 2 spines
- **Ports:** 32x100GbE each
- **Role:** Aggregating both leafs AND direct ToR connections (mixed life)

### Leaf Layer (Pod 1 Only)

- **Model:** Edgecore 7726-32X-O
- **Role:** Rack-level aggregation

### ToR Layer

- **Model:** Edgecore 7726-32X-O
- **Role:** Server connectivity

## Quick Start

```bash
# really quick
uv run inv deploy-dc --scenario dc4 --branch your_branch

# I'm the control nerd
uv run infrahubctl branch create you_branch

# Load topology (this is the point of no return)
uv run infrahubctl object load data/demos/01_data_center/dc4/ --branch you_branch

# Generate fabric (grab coffee, this might take a while)
uv run infrahubctl generator generate_dc name=DC4 --branch you_branch

```

Trigger infrastructure generation in InfraHub UI → Actions → Generator Definitions → generate_dc DC4-Fabric-1

## Fun Fact

The author owns a piece of the Berlin Wall—so if your network ever feels divided, just remember: it can be rebuilt, repurposed, or turned into a conversation starter at tech meetups. It’s a daily reminder that even the toughest partitions eventually fall—sometimes with a little help from automation, sometimes with a sledgehammer.

Bonus: The author proudly benefits from Germany’s Unity Day, enjoying a free holiday every year thanks to history and a chunk of concrete.

Prost to open borders, open networks, and open source!
