# DC3 - Brexit, No Middle Management, Maximum Sass

## Overview
**Location:** London 🇬🇧 (Brexit happened, but your data stays! More fiber under the Thames than umbrellas in the city)

**Size:** Small (S) - Minimalist, fast, and direct

**Platform:** Dell PowerSwitch with SONiC - Open networking, ultra-low latency

**Design Pattern:** S-Flat-ToR (Small Flat Top-of-Rack)

**Use Case:**
For those who say "I don't want any extra hops" and actually mean it. Pure flat ToR deployment—every ToR switch connects directly to spines, like a networking speed-dating event with zero commitment. No middle aggregation, no leaf layer bureaucracy, just servers talking to ToRs and ToRs talking to spines. It's a minimalist's dream and a cable management team's recurring nightmare. If you love low latency, hate complexity, and enjoy watching your spine ports disappear faster than free beer at a tech conference, this one's for you. Warning: May cause spontaneous outbreaks of optimism and existential dread in equal measure.

---

## Architecture (Flatness with British Charm)

### Fabric Scale
- **Super Spines:** 2 (Dell PowerSwitch S5232F-ON)
- **Total Pods:** 2
- **Total Spines:** 4 (2 per pod)
- **Total Racks:** 4
- **Deployment Type:** tor (both pods)

### Pod Structure (Zero-Middle-Layer Society)
| Pod   | Spines | Model                | Racks | Deployment | Personality         |
|-------|--------|----------------------|-------|------------|---------------------|
| Pod 1 | 2      | S5232F-ON           | 2     | tor        | The Speed Racer     |
| Pod 2 | 2      | S5232F-ON           | 2     | tor        | The Speed Racer's Twin |

---

## Hardware Stack (Simplicity Through SONiC)

### Super Spine Layer
- **Model:** Dell PowerSwitch S5232F-ON
- **Ports:** 32x100GbE
- **Role:** Inter-pod autobahn
- **Fun Fact:** Flat ToR is how you win arguments about east-west latency

### Spine Layer
- **Model:** Dell PowerSwitch S5232F-ON
- **Ports:** 32x100GbE
- **Role:** Direct ToR aggregation
- **Deployment:** Identical across pods

### ToR Layer
- **Model:** Dell PowerSwitch S5232F-ON
- **Count:** 2 per rack
- **Role:** Server connectivity

---

## Deployment Strategy (Flat ToR Mastery)

**ToR Connectivity Pattern:**
```
Server → ToR → Spine → Super Spine
```
---

## Quick Start

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

Unlike certain monarchs, this mug has never abdicated, and it’s still on the throne of morning coffee—no royal drama, just reliable caffeine delivery.
