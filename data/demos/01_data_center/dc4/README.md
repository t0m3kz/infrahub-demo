# DC4 - Extra Large (Hyper-Spine Tier)

"Super" was already taken, so the fabric reached for "hyper", as one does in Berlin after midnight.

## Overview

**Location:** Berlin, Germany

**Platform:** Arista EOS at the DC level

**Fabric Design:** `XL` + `ebgp-ebgp` routing — the same eBGP-eBGP pattern as DC1/DC3, but Extra Large,
which adds a 4th tier: **hyper-spine**. Leaf → spine → super-spine →
hyper-spine, full-mesh cabled and routed between super-spine and hyper-spine (every
super-spine talks to every hyper-spine, same fan-out logic as spine↔super-spine, one
tier higher — and dc.py cables this pair itself, since both tiers live at the DC level).

**Use Case:** When 4 super-spines become a chokepoint and one more
level of aggregation is needed. Pods deliberately use mixed deployment types
(middle_rack, mixed and tor) to demonstrate that deployment type is chosen per pod.

---

## Architecture

### Fabric Scale

- **Hyper-Spines:** 2 (Arista DCS-7050CX3-32C-R) — full mesh to every super-spine, top tier of the fabric
- **Super-Spines:** 4 (Arista DCS-7050CX3-32C-R)
- **Border-Leaf:** 2 (Arista DCS-7050CX3-32C-R) — one each in pods 1 and 2, own firewall + load-balancer pair
- **Pods:** 3, deliberately mixed deployment types

| Pod | Spines | Design   | Deployment  |
| --- | ------ | -------- | ----------- |
| 1   | 2      | S_MIDDLE | middle_rack |
| 2   | 4      | S_MIXED  | mixed       |
| 3   | 4      | S_TOR    | tor         |

### Tier Summary

```text
Leaf → Spine → Super-Spine ⇄ Hyper-Spine (full mesh, DC-level, cabled by dc.py itself)
                   |
              Border-Leaf → Firewall → Load-Balancer (independent legs, PBR)
```

## Quick Start

```bash
# See Deployment Steps below for the current load and generator commands.
```

## Deployment Steps

```bash
# Quick path
# See the manual commands below, or use the automatic created-object trigger.

# Manual path
uv run infrahubctl branch create you_branch

# Load topology
uv run infrahubctl object load data/demos/01_data_center/dc4/ --branch you_branch

# Generate fabric
uv run infrahubctl generator add_dc name=DC4 --branch you_branch

```

Alternatively, trigger the generator from the InfraHub UI → Actions → Generator Definitions → add_dc for DC4.

## Validation

Run `uv run invoke test-integration-routing` before committing generator-ordering changes, then use `uv run invoke test-integration` for the complete mixed-topology validation.
