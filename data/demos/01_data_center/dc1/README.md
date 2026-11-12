# DC1 - Classic 3-Tier (Small Size)

The fabric your auditor dreams about, and the one your architect calls "a bit boring" in every meeting.

## Overview

**Location:** Munich, Germany

**Platform:** Cisco Nexus 9K

**Fabric Design:** `L` (small instance) + `ebgp-ebgp` routing — pure eBGP underlay + eBGP overlay (RFC 7938), IPv6 P2P links.
This is the reference fabric: leaf → spine → super-spine, DC-level border-leaf fronted by a
firewall/load-balancer pair. It uses no back-to-back links, no collapsed roles and no mixed vendors:
it is the standard 3-tier Clos that the other DCs in this fleet are variations on.

**Use Case:** Reference design of a standard small data center (2 pods) and the baseline for the other DC scenarios.

---

## Architecture

### Fabric Scale

- **Super-Spines:** 2 (Cisco N9K-C9336C-FX2)
- **Border-Leaf:** 2 (Cisco N9K-C9336C-FX2), with its own firewall + load-balancer pair
- **Pods:** 2 | **Spines:** 4 (2+2) | **Racks:** 4

| Pod | Spines | Design   | Deployment  |
| --- | ------ | -------- | ----------- |
| 1   | 2      | S_MIDDLE | middle_rack |
| 2   | 2      | S_MIDDLE | middle_rack |

### Tier Summary

```text
Leaf → Spine → Super-Spine
                   |
              Border-Leaf → Firewall → Load-Balancer (inline chain)
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
uv run infrahubctl object load data/demos/01_data_center/dc1/ --branch you_branch

# Generate fabric (this might take a while)
uv run infrahubctl generator add_dc name=DC1 --branch you_branch
```

Alternatively, trigger infrastructure generation in InfraHub UI → Actions → Generator Definitions → add_dc for DC1.

## Validation

Deploy it as a validated, merged change with `uv run invoke kick dc1` (all seven DCs: `uv run invoke kick dc-deployments`).
Use `uv run invoke test-integration` for the complete DC matrix. The harness waits for cascading tasks and names any branch that does not settle.
