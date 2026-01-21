# DC3 - Brexit, Flat ToR, Maximum Speed

## Overview

**Location:** London 🇬🇧 | **Size:** Small | **Platform:** Dell SONiC | **Design:** `spine-leaf-tor-4spine`

Brexit happened, but your data stays! Pure flat ToR—every ToR connects directly to spines, zero middle layers, zero bureaucracy. Networking speed dating with commitment issues. If you love low latency and hate explaining why you need a leaf layer, this is your DC.

**Philosophy:** "I don't want any extra hops" (and we mean it).

## Architecture

- **Super Spines:** 2 (Dell S5232F-ON)
- **Pods:** 2 | **Spines:** 8 (4+4) | **Racks:** 4
- **Deployment:** `tor` (both pods) - Direct spine connections or bust

| Pod | Spines | Design                | Site Layout | Personality         |
| --- | ------ | --------------------- | ----------- | ------------------- |
| 1   | 4      | spine-leaf-tor-4spine | small-dc    | Speed Demon         |
| 2   | 4      | spine-leaf-tor-4spine | small-dc    | Speed Demon's Clone |

## Quick Start

```bash
uv run inv deploy-dc --scenario dc3 --branch your_branch
```

**Warning:** Spine port consumption rates may cause existential dread. Low latency worth it

---

## Deployment Strategy (Flat ToR Mastery)

**ToR Connectivity Pattern:**

```bash
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

and follow steps from dc1

## Fun Fact

The author still uses the mug he bought 25 years ago in London—proof that some British imports last longer than most celebrity marriages, and definitely longer than any network outage.

Unlike certain monarchs, this mug has never abdicated, and it’s still on the throne of morning coffee—no royal drama, just reliable caffeine delivery.
