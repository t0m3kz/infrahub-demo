# 07 DC Security — Bouncers, Inspectors, and Traffic Cops

## Overview

This folder contains firewall and load-balancer endpoint definitions for all six data centers.

It lives **separately from** `01_data_center/` for one reason: the border-leaf switches that these
appliances plug into must already exist before you can load this data. Build the DC first, then
hire the bouncers.

**Loading order (non-negotiable):**

```text
01_data_center/dcN/  →  generate_dc  →  generate racks  →  07_dc_security/dcN_*.yml  →  add_endpoint
```

---

## Hardware Per DC

| DC | Location | Firewall | Vendor      | Load Balancer            | Vendor |
|----|----------|----------|-------------|--------------------------|--------|
| 1  | Munich   | CP-26000 | Check Point | BIG-IP-i5800             | F5     |
| 2  | Paris    | PA-5260  | Palo Alto   | NetScaler-MPX-26000-100G | Citrix |
| 3  | London   | CP-26000 | Check Point | BIG-IP-i5800             | F5     |
| 4  | Berlin   | CP-26000 | Check Point | NetScaler-MPX-26000-100G | Citrix |
| 5  | New York | PA-5260  | Palo Alto   | BIG-IP-i5800             | F5     |
| 6  | Katowice | CP-26000 | Check Point | NetScaler-MPX-26000-100G | Citrix |

---

## Architecture

### The Service Chain

```text
Internet
    │
Border-Leaf pair (BL-1, BL-2)
    │
Firewall (active/passive)   ← Check Point ClusterXL / PAN HA
    │
Load Balancer (active/passive)  ← F5 DSC / Citrix HA mode
    │
Compute Leafs → Servers
```

### Border-Leaf Pair — No VPC

Each DC has exactly **two border-leafs**, one per pod, in separate racks. They are fully independent
— no peer-link, no shared control plane, no VPC domain. BGP ECMP on the spines handles redundancy.
Losing one BL is a routing convergence event, not a war room call.

The BL templates have dedicated port zones:

| Zone            | Ports (NX-OS example) | Purpose                     |
|-----------------|-----------------------|-----------------------------|
| `firewall`      | E1/25–28              | FW uplink attachment        |
| `load-balancer` | E1/29–32              | LB uplink attachment        |
| `downlink`      | E1/1–24               | Spine connections           |
| `uplink`        | E1/33–36              | DCI / external connectivity |

### Dual-Uplink Deterministic Assignment

Each appliance has two uplink interfaces. The endpoint generator assigns them deterministically:

```text
uplink[0] (sorted by name)  →  BL-1 (sorted by name)
uplink[1]                   →  BL-2
```

No locking, no race condition — the assignment is pure computation based on sorted names.
Two generators running simultaneously for different appliances always pick different ports.

### Redundancy Without VPC

Both FW and LB run **active/passive** HA via their native appliance protocol:

| Appliance            | HA Protocol                     | Failover Mechanism        |
|----------------------|---------------------------------|---------------------------|
| Check Point CP-26000 | ClusterXL                       | GARP — BLs follow the MAC |
| Palo Alto PA-5260    | PAN HA1/HA2                     | GARP — BLs follow the MAC |
| F5 BIG-IP-i5800      | Device Service Clustering (DSC) | GARP                      |
| Citrix NetScaler MPX | HA mode                         | GARP — BLs follow the MAC |

When the active unit fails, the standby takes the floating VIP, sends a Gratuitous ARP, and both
BLs update their ARP tables. The fabric reconverges. No network-level coordination needed.

### Static Routes + BFD + PBR on BLs

No BGP on the appliances — that would be overkill for active/passive boxes that aren't advertising
prefixes. Instead the border-leafs handle it all:

- **Static route** → reachability to the FW/LB VIP
- **BFD** → sub-second failure detection (kicks in when GARP alone isn't fast enough)
- **PBR** → forces traffic through the FW regardless of the routing table (service insertion)

---

## Loading Instructions

### Single DC (quick)

```bash
DC=dc1
BRANCH=your_branch

# Step 1 — build DC topology and fabric
uv run infrahubctl object load data/demos/01_data_center/${DC}/ --branch ${BRANCH}
uv run infrahubctl generator generate_dc name=DC1 --branch ${BRANCH}

# Step 2 — attach security layer
uv run infrahubctl object load data/demos/07_dc_security/${DC}_firewalls.yml --branch ${BRANCH}
uv run infrahubctl object load data/demos/07_dc_security/${DC}_load_balancers.yml --branch ${BRANCH}

# Step 3 — wire the appliances to border-leafs
uv run infrahubctl generator add_endpoint name=${DC}-fw-01 --branch ${BRANCH}
uv run infrahubctl generator add_endpoint name=${DC}-lb-01 --branch ${BRANCH}
```

### All DCs at once

```bash
for DC in dc1 dc2 dc3 dc4 dc5 dc6; do
  uv run infrahubctl object load data/demos/07_dc_security/${DC}_firewalls.yml --branch ${BRANCH}
  uv run infrahubctl object load data/demos/07_dc_security/${DC}_load_balancers.yml --branch ${BRANCH}
done
```

---

## Files

| File                     | Device        | DC | Rack              |
|--------------------------|---------------|----|-------------------|
| `dc1_firewalls.yml`      | `muc-1-fw-01` | 1  | `muc-1-s-1-r-5-1` |
| `dc1_load_balancers.yml` | `muc-1-lb-01` | 1  | `muc-1-s-1-r-5-1` |
| `dc2_firewalls.yml`      | `par-1-fw-01` | 2  | `par-1-s-1-r-3-1` |
| `dc2_load_balancers.yml` | `par-1-lb-01` | 2  | `par-1-s-1-r-3-1` |
| `dc3_firewalls.yml`      | `lon-1-fw-01` | 3  | `lon-1-s-1-r-3-1` |
| `dc3_load_balancers.yml` | `lon-1-lb-01` | 3  | `lon-1-s-1-r-3-1` |
| `dc4_firewalls.yml`      | `ber-1-fw-01` | 4  | `ber-1-s-1-r-3-1` |
| `dc4_load_balancers.yml` | `ber-1-lb-01` | 4  | `ber-1-s-1-r-3-1` |
| `dc5_firewalls.yml`      | `ny-1-fw-01`  | 5  | `ny-1-s-1-r-3-1`  |
| `dc5_load_balancers.yml` | `ny-1-lb-01`  | 5  | `ny-1-s-1-r-3-1`  |
| `dc6_firewalls.yml`      | `ktw-1-fw-01` | 6  | `ktw-1-s-1-r-3-5` |
| `dc6_load_balancers.yml` | `ktw-1-lb-01` | 6  | `ktw-1-s-1-r-3-5` |

---

## Fun Fact

The author spent considerable time debating whether VPC was needed here.
The answer was no. Twice. Then yes. Then no again.
Active/passive HA and GARP have been solving this problem since before VPC existed.
Sometimes the old ways are old because they work.
