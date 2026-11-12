# DC6 - Mixed Vendors and Deployment Types

One kitchen, four vendors, and a second helping of support contracts nobody ordered.

## Overview

**Location:** Katowice, Poland

**Platform:** Multi-Vendor (Cisco, Arista, Dell SONiC, Edgecore SONiC)

**Fabric Design:** `L` (small instance) + `ebgp-ibgp` routing — eBGP underlay + iBGP overlay, IPv6 P2P links. 3 pods.
The eBGP underlay supports per-pod ASNs, which a multi-vendor fabric needs, and avoids relying on an IGP
behaving identically across vendors. The iBGP overlay uses the existing super-spines as route reflectors.
The underlay is IPv6.

**Use Case:**
Medium-sized multi-vendor data center with middle_rack, tor and mixed deployment types. It demonstrates
vendor interoperability (Cisco, Arista, Dell and Edgecore) at a scale suited to medium enterprises.

## Architecture

- **Super Spines:** 2 (Cisco N9K-C9336C-FX2)
- **Pods:** 3 | **Spines:** 6 (2+2+2) | **Racks:** 12
- **Deployment:** `middle_rack`, `tor`, `mixed` — one of each

| Pod | Spines | Design   | Deployment  | Spine Vendor | Site Layout |
| --- | ------ | -------- | ----------- | ------------ | ----------- |
| 1   | 2      | M_MIDDLE | middle_rack | Cisco        | small-dc    |
| 2   | 2      | S_TOR    | tor         | Cisco        | small-dc    |
| 3   | 2      | M_MIXED  | mixed       | Cisco        | small-dc    |

## Quick Start

```bash
# See Deployment Steps below for the current load and generator commands.
```

---

## Alternative Quick Start

```bash
# Quick path
# See the manual commands below, or use the automatic created-object trigger.

# Manual path
uv run infrahubctl branch create you_branch

# Load topology
uv run infrahubctl object load data/demos/01_data_center/dc6/ --branch you_branch

# Generate fabric (this might take a while)
uv run infrahubctl generator add_dc name=DC6 --branch you_branch

```

Alternatively, trigger infrastructure generation in InfraHub UI → Actions → Generator Definitions → add_dc for DC6.

## Validation

Run `uv run invoke kick dc6` to deploy the mixed-vendor topology as a validated, merged change; `dc6-growth` and `dc6-customers` build on it. The integration suite's `dc6-suite` kick (`tests/integration/kicks/dc6.yml`, run by `test_04_kicks.py`) runs the same kick with assertions per stage.
