# DC5 - Multi-Vendor (Four Vendors, One Fabric)

Four vendors walk into a fabric. Their support hotlines are still arguing about who walked in first.

## Overview

**Location:** New York, United States

**Platform:** Multi-Vendor (Cisco, Arista, Dell, Edgecore)

**Fabric Design:** `L` (small instance) + `ebgp-ebgp` routing — eBGP underlay + eBGP overlay (RFC 7938), IPv6 P2P links.
Using eBGP throughout keeps the routing behaviour identical across the four vendors in the same fabric
(no IGP differences between platforms). The underlay is IPv6, the one addressing choice common to all four vendors.

**Use Case:**
Pod-level vendor diversity for risk mitigation and best-of-breed selection. Each pod uses a
different vendor. The deployment is middle_rack across all 4 pods. Suited to testing vendor
migration strategies and comparing vendor platforms in one fabric.

---

## Architecture

### Fabric Scale

- **Super Spines:** 2 (Cisco N9K-C9336C-FX2)
- **Pods:** 4 | **Spines:** 8 (2+2+2+2) | **Racks:** 8
- **Deployment:** `middle_rack` (all pods)

| Pod | Spines | Spine Vendor | Design   | Deployment  | Site Layout |
| --- | ------ | ------------ | -------- | ----------- | ----------- |
| 1   | 2      | Cisco        | S_MIDDLE | middle_rack | small-dc    |
| 2   | 2      | Arista       | S_MIDDLE | middle_rack | small-dc    |
| 3   | 2      | Dell         | S_MIDDLE | middle_rack | small-dc    |
| 4   | 2      | Edgecore     | S_MIDDLE | middle_rack | small-dc    |

## Quick Start

```bash
# See Deployment Steps below for the current load and generator commands.
```

### ToR Layer

- **Model:** Varies by pod
- **Role:** Server connectivity

## Deployment Steps

```bash
# Quick path
# See the manual commands below, or use the automatic created-object trigger.

# Manual path
uv run infrahubctl branch create you_branch

# Load topology
uv run infrahubctl object load data/demos/01_data_center/dc5/ --branch you_branch

# Generate fabric (this might take a while)
uv run infrahubctl generator add_dc name=DC5 --branch you_branch

```

Alternatively, trigger infrastructure generation in InfraHub UI → Actions → Generator Definitions →
add_dc for DC5.

## Validation

Use `uv run invoke kick dc-deployments` for all seven DCs. For automatic DC/POD/rack ordering changes, start with `uv run invoke kick dc5`.
