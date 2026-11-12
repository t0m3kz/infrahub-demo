# DC7 - Micro-Fabric (Collapsed Border-Spine)

Two jobs, one box, and a bike parked outside the rack.

## Overview

**Location:** Amsterdam, Netherlands

**Platform:** Arista EOS

**Fabric Design:** `S` + `ebgp-ebgp` routing — the canonical micro-fabric pattern: no super-spine tier
at all (back-to-back — pods mesh their own **border-spines** directly to sibling pods), and no
DC-level border-leaf/firewall/load-balancer tier either. Every pod's border-spine collapses
spine + border-leaf into a single device, and carries its own dedicated firewall/load-balancer
pair. Two device roles in one box means one fewer tier to cable and patch.

**Use Case:** When a pod is small enough (about 8 leafs) that a dedicated super-spine tier and
a separate border-leaf are unnecessary overhead. DC7 is the reference example for this pattern —
DC2/DC3/DC4 previously demonstrated variants of it across three vendors and have since been retired.

---

## Architecture

### Fabric Scale

- **Border-Spines:** 4 (2+2, Arista DCS-7050CX3-32C-R) — collapsed spine + border-leaf, one pair per pod
- **Pods:** 2 | **Racks:** 2 (1 network rack per pod, 8 leafs each)
- **Deployment:** `middle_rack` (both pods), back-to-back mesh between pods (no super-spine tier)

| Pod | Border-Spines | Design | Deployment | Own FW/LB |
| --- | -------------- | ----------------- | ----------- | --------- |
| 1 | 2 | S_BORDER_SPINE_POD | middle_rack | yes |
| 2 | 2 | S_BORDER_SPINE_POD | middle_rack | yes |

### Tier Summary

```text
Leaf → Border-Spine ⇄ Border-Spine (every pod, full mesh — no super-spine tier)
              |
        Firewall → Load-Balancer (this pod's own pair, PBR)
```

## Quick Start

```bash
# See Deployment Steps below for the current load and generator commands.
```

**Note:** This design has no separate border-leaf tier; its function is part of the border-spine.

## Deployment Steps

```bash
# Quick path
# See the manual commands below, or use the automatic created-object trigger.

# Manual path
uv run infrahubctl branch create you_branch

# Load topology
uv run infrahubctl object load data/demos/01_data_center/dc7/ --branch you_branch

# Generate fabric (this might take a while)
uv run infrahubctl generator add_dc name=DC7 --branch you_branch

```

Alternatively, trigger infrastructure generation in InfraHub UI → Actions → Generator Definitions → add_dc for DC7.

## Validation

Run `uv run invoke test-integration-routing` to validate the collapsed border-spine path and automatic routing cascade. Use `uv run invoke test-integration` for the complete topology matrix.
