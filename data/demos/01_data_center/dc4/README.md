# DC4 - Extra Large: When Super-Spine Isn't Enough

## Overview

**Location:** Berlin 🇩🇪 (The hipster capital - your infrastructure is as edgy as the local techno scene)

**Platform:** Arista EOS at the DC level — so API-driven, even the hyper-spine tier could
probably deploy itself if you left the terminal unattended.

**Fabric Design:** `XL` + `ebgp-ebgp` routing — the same eBGP-eBGP pattern as DC1/DC3, but Extra Large,
which means the fabric grew a 4th tier: **hyper-spine**. Leaf → spine → super-spine →
hyper-spine, full-mesh cabled and routed between super-spine and hyper-spine (every
super-spine talks to every hyper-spine, same fan-out logic as spine↔super-spine, just one
tier higher — and dc.py cables this pair itself, since both tiers live at the DC level).

**Use Case:** When 4 super-spines start feeling like a chokepoint and you need one more
level of aggregation before things get truly hyperscale. Also: proof that "wymieszane, co
jest dostępne" (mixed, whatever's available) is a legitimate pod strategy when your fabric
is big enough that nobody's going to audit every pod's deployment type anyway.

---

## Architecture (One Tier Higher Than Everyone Else)

### Fabric Scale

- **Hyper-Spines:** 2 (Arista DCS-7050CX3-32C-R) — full mesh to every super-spine, nothing above them
- **Super-Spines:** 4 (Arista DCS-7050CX3-32C-R)
- **Border-Leaf:** 4 (Arista DCS-7050CX3-32C-R), own firewall + load-balancer pair
- **Pods:** 3, deliberately mixed deployment types

| Pod | Spines | Design   | Deployment  | Personality                         |
| --- | ------ | -------- | ----------- | ----------------------------------- |
| 1   | 2      | S_MIDDLE | middle_rack | The Overachiever                    |
| 2   | 4      | S_MIXED  | mixed       | Can't commit to one rack type       |
| 3   | 4      | S_TOR    | tor         | Skipped the middle-management layer |

### Tier Summary

```text
Leaf → Spine → Super-Spine ⇄ Hyper-Spine (full mesh, DC-level, cabled by dc.py itself)
                   |
              Border-Leaf → Firewall → Load-Balancer (independent legs, PBR)
```

## Quick Start

```bash
uv run inv deploy-dc --scenario dc4 --branch your_branch
```

**Warning:** May cause identity crisis. Perfect for flexing multi-deployment AND
multi-tier skills at the same time.

## Deployment Steps

```bash
# really quick
uv run inv deploy-dc --scenario dc4 --branch your_branch

# I'm the control nerd
uv run infrahubctl branch create you_branch

# Load topology (this is the point of no return)
uv run infrahubctl object load data/demos/01_data_center/dc4/ --branch you_branch

# Generate fabric (grab coffee)
uv run infrahubctl generator generate_dc name=DC4 --branch you_branch

```

Trigger infrastructure generation in InfraHub UI → Actions → Generator Definitions → generate_dc DC4-Fabric-1

## Fun Fact

The author owns a piece of the Berlin Wall—so if your network ever feels divided, just remember: it can be
rebuilt, repurposed, or turned into a conversation starter at tech meetups. It's a daily reminder that even
the toughest partitions eventually fall—sometimes with a little help from automation, sometimes with a
sledgehammer.

Bonus: The author proudly benefits from Germany's Unity Day, enjoying a free holiday every year thanks to
history and a chunk of concrete.

Prost to open borders, open networks, and open source!
