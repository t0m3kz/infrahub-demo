# DC7 - Micro-Fabric: One Device, No Excuses

Two jobs, one box, and a bike parked outside the rack.

## Overview

**Location:** Amsterdam 🇳🇱 (Canals, bikes, and a fabric small enough to fit in a houseboat, though we do not recommend testing that)

**Platform:** Arista EOS - collapsing two device roles into one and somehow still finding time
for a bike ride. The box works overtime so the Dutch don't have to.

**Fabric Design:** `S` + `ebgp-ebgp` routing — the canonical micro-fabric pattern: no super-spine tier
at all (back-to-back — pods mesh their own **border-spines** directly to sibling pods), and no
DC-level border-leaf/firewall/load-balancer tier either. Every pod's border-spine collapses
spine + border-leaf into a single device, and carries its own dedicated firewall/load-balancer
pair. Two device roles, one box, one fewer tier to cable, patch, and lose sleep over. (Fewer tiers also means fewer places for the outage to hide. Allegedly.)

**Use Case:** When a pod is small enough (think 8 leafs) that a dedicated super-spine tier and
a separate border-leaf are pure overhead. DC7 is the reference example for this pattern —
DC2/DC3/DC4 used to demo variants of it across three vendors; this is the one that stays. The others have been retired with full honors and no ceremony.

---

## Architecture (Two Roles, One Device)

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

**Warning:** May cause you to ask "wait, where's the border-leaf?" — there isn't one, that's
the whole point. Ask once, then read the Architecture section again.

## Deployment Steps

```bash
# really quick
# See the manual commands below, or use the automatic created-object trigger.

# I'm the control nerd
uv run infrahubctl branch create you_branch

# Load topology (this is the point of no return)
uv run infrahubctl object load data/demos/01_data_center/dc7/ --branch you_branch

# Generate fabric (grab coffee, this might take a while)
uv run infrahubctl generator add_dc name=DC7 --branch you_branch

```

Or trigger infrastructure generation in InfraHub UI → Actions → Generator Definitions → add_dc for DC7. Same generator, now with clicking.

## Validation

Run `uv run invoke test-integration-routing` to validate the collapsed border-spine path and automatic routing cascade (the one box that does two jobs had better be tested twice). Use `uv run invoke test-integration` for the complete topology matrix.

## Fun Fact

Amsterdam has more bikes than people, which is roughly the same ratio border-spine devices
have of "roles collapsed" to "actual physical boxes" — efficiency, but the Dutch way (and, unlike the bikes, nobody has fished these out of a canal yet).
