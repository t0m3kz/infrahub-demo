# DC1 - The Textbook: Classic 3-Tier, Small Size

## Overview

**Location:** Munich 🇩🇪 (Home to Oktoberfest and BMW - where beer gardens meet precision engineering)

**Platform:** Cisco Nexus 9K - The networking equivalent of driving a tank to the grocery store, but at least it's a *predictable* tank.

**Fabric Design:** `L` (small instance) + `ebgp-ebgp` routing — pure eBGP underlay + eBGP overlay (RFC 7938), IPv6 P2P links.
This is the reference fabric: leaf → spine → super-spine, DC-level border-leaf fronted by a
firewall/load-balancer pair. No back-to-back tricks, no collapsed roles, no mixed vendors —
just the textbook 3-tier Clos every other DC in this fleet is a variation on.

**Use Case:** When someone asks "what does a normal DC actually look like?" — point them here.
Small (2 pods), boring, correct. The control group of the experiment.

---

## Architecture (Textbook Edition)

### Fabric Scale

- **Super-Spines:** 2 (Cisco N9K-C9336C-FX2) — the top of this fabric, nothing above them
- **Border-Leaf:** 2 (Cisco N9K-C9336C-FX2), with its own firewall + load-balancer pair
- **Pods:** 2 | **Spines:** 4 (2+2) | **Racks:** 4

| Pod | Spines | Design   | Deployment  | Personality         |
| --- | ------ | -------- | ----------- | ------------------- |
| 1   | 2      | S_MIDDLE | middle_rack | The Responsible One |
| 2   | 2      | S_MIDDLE | middle_rack | The Backup Plan     |

### Tier Summary

```text
Leaf → Spine → Super-Spine
                   |
              Border-Leaf → Firewall → Load-Balancer (inline chain)
```

## Quick Start

```bash
uv run inv deploy-dc --scenario dc1 --branch your_branch
```

**Warning:** May cause a feeling of "wait, that's it?" — yes, that's the point.

## Deployment Steps

```bash
# really quick
uv run inv deploy-dc --scenario dc1 --branch your_branch

# I'm the control nerd
uv run infrahubctl branch create you_branch

# Load topology (this is the point of no return)
uv run infrahubctl object load data/demos/01_data_center/dc1/ --branch you_branch

# Generate fabric (grab coffee, this might take a while)
uv run infrahubctl generator generate_dc name=DC1 --branch you_branch
```

Trigger infrastructure generation in InfraHub UI → Actions → Generator Definitions → generate_dc DC1-Fabric-1

## Fun Fact

The author lives in Munich and has spent years trying to understand the rules of Schafkopf as explained in Bavarian—proof that some network topologies are easier to decipher than local card games.

If you ever win a round, you're officially more Bavarian than a pretzel at Oktoberfest.

Munich Fact: The city's official symbol is the Münchner Kindl—a child in a monk's robe, which is still less mysterious than Bavarian pronunciation (especially after your second Maß of beer).
