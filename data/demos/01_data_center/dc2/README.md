# DC2 - Back-to-Back (No Super-Spine)

Flat like a crêpe where it counts, layered like a mille-feuille where it matters.

## Overview

**Location:** Paris, France

**Platform:** Arista EOS

**Fabric Design:** `M` + `ebgp-ebgp` routing — no super-spine tier at all: every pod's spines mesh
directly with every other pod's spines (full mesh, not a star through an aggregation
layer above them). Unlike the micro-fabric border-spine pattern, DC2 keeps a separate
**border-leaf** tier at the DC level, fronted by its own firewall/load-balancer pair;
no roles are collapsed into a single device. eBGP underlay + eBGP overlay, IPv6 P2P links.

**Use Case:** Saves the ports and latency of a super-spine tier without collapsing spine and border-leaf
into one device as the border-spine pattern does. DC2 shows that "skip the super-spine tier"
and "keep border-leaf separate" are independent design choices.

---

## Architecture

### Fabric Scale

- **Border-Leaf:** 2 (Arista DCS-7050CX3-32C-R), with its own firewall + load-balancer pair (a separate tier)
- **Pods:** 4 | **Spines:** 8 (2 per pod) | **Racks:** 4
- **Mesh:** every pod's spines connect directly to every other pod's spines — no super-spine
  tier to route through, no DC-level orchestration required (each pod cables itself to its
  existing lower-index siblings as soon as it comes up)

| Pod | Spines | Design   | Deployment  | Mesh                 |
| --- | ------ | -------- | ----------- | -------------------- |
| 1   | 2      | S_MIDDLE | middle_rack | First pod            |
| 2   | 2      | S_MIDDLE | middle_rack | Meshes with Pod 1    |
| 3   | 2      | S_MIDDLE | middle_rack | Meshes with 1 & 2    |
| 4   | 2      | S_MIDDLE | middle_rack | Meshes with 1, 2 & 3 |

### Tier Summary

```text
Leaf → Spine ⇄ Spine (every pod, full mesh — no super-spine tier)
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
uv run infrahubctl object load data/demos/01_data_center/dc2/ --branch you_branch

# Generate fabric (this might take a while)
uv run infrahubctl generator add_dc name=DC2 --branch you_branch

```

Alternatively, trigger infrastructure generation in InfraHub UI → Actions → Generator Definitions → add_dc for DC2.

## Validation

Run `uv run invoke kick dc2` to deploy this eBGP/eBGP, no-super-spine design as a validated, merged change, or `uv run invoke kick dc-deployments` for all seven DCs.
