# DC3 - Brexit, Flat ToR, Maximum Speed

## Overview

**Location:** London 🇬🇧 | **Size:** Small | **Platform:** Dell SONiC | **Design:** `S_OSPF_IBGP`

**Fabric Design:** `S_OSPF_IBGP` — OSPF underlay + iBGP overlay, IPv6 P2P links (with the flat naming
convention because London hates unnecessary formality). OSPF floods LSAs across the underlay while
iBGP distributes EVPN routes via spine route reflectors — a division of labour as British as the
House of Lords. At least they upgraded to IPv6 underlay. Brexit happened, but the addressing did not
regress. Small wins.

Brexit happened, but your data stays! Mixed deployment across both pods — ToR and middle rack coexist in
suspicious harmony, just like English and metric units on the same road sign.

**Philosophy:** "I don't want any extra hops" (but I'll take a few anyway, it's fine).

## Architecture

- **Super Spines:** 2 (Dell PowerSwitch S5232F-ON)
- **Pods:** 2 | **Spines:** 4 (2+2) | **Racks:** 8
- **Deployment:** `mixed` (both pods) - The Brexit compromise: some structure, some freedom

| Pod | Spines | Design  | Deployment | Site Layout | Personality         |
| --- | ------ | ------- | ---------- | ----------- | ------------------- |
| 1   | 2      | S_MIXED | mixed      | small-dc    | Pragmatic Londoner  |
| 2   | 2      | S_MIXED | mixed      | small-dc    | Same Pragmatic Twin |

## Quick Start

```bash
uv run inv deploy-dc --scenario dc3 --branch your_branch
```

**Warning:** Spine port consumption rates may cause existential dread. Low latency worth it

---

## Deployment Strategy (Mixed — The British Compromise)

**ToR Connectivity Patterns:**

```bash
# Some racks go direct (the ToR contingent)
Server → ToR → Spine → Super Spine

# Other racks add a leaf layer (the middle-rack contingent)
Server → ToR → Leaf → Spine → Super Spine
```

---

## Alternative Quick Start

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
