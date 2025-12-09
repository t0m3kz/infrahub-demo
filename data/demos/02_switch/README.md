# 02 - Additional Switches Demo

*When You Just Need Two More Switches (Famous Last Words)*

## Overview

**Purpose:** Add 2 ToRs to an existing rack in DC1's Pod 1. Because "temporary" upgrades are forever.

**Philosophy:** "We just need a FEW more switches" - said everyone before their topology doubled and their cable management became performance art.

**Difficulty:** Easy (until you realize you're modifying production infrastructure and the CFO is watching).

---

## What's Inside

Enhancing existing rack (`muc-1-s-1-r-1-1`) with:

- **+2x Cisco ToRs** (N9K-C9336C-FX2) - More ToRs than a fantasy trilogy marathon
- **Location:** Munich DC1, Pod 1, Suite 1, Row 1 - Where it all began (and will never end)

---

## Use Case

Perfect for when your team says:

- "Can we just add TWO more switches?" (Narrator: It's never just two)
- "The existing rack has capacity" (Until it doesn't)
- "Quick expansion" (Spoiler: It becomes permanent)
- "We can squeeze them in" (Until power/cooling becomes a problem)
- "Just upgrade the existing rack" (Before the CFO asks why we didn't plan better)

---

## Deployment

```bash
uv run infrahubctl branch create your_branch
uv run infrahubctl object load data/demos/02_switch/ --branch your_branch
```

Trigger infrastructure generation in InfraHub UI → Actions → Generator Definitions → generate_rack MUC-1-SUITE-1-R1-5

---

## Fun Fact

Every "temporary" rack addition is a permanent fixture by next quarter.

The only thing multiplying faster than switches is regret.

Cable management will require a PhD and a sense of humor.

The only thing more organic than this rack expansion is the panic when someone asks, "Can we add just two more switches?"

If you think this is chaotic, wait until someone suggests moving it all to the cloud.

Tip : Public cloud - because nothing says "future-proof" like paying monthly to rent someone else's chaos.
