# 02 - Additional Switches Demo

When you just need two more switches (famous last words)

## Overview

**Purpose:** Add switches to an existing network rack in DC6's Pod 1. Because "temporary" upgrades are forever.

**Philosophy:** "We just need a FEW more switches" - said everyone before their topology doubled and their cable management became performance art.

**Difficulty:** Easy (until you realize you're modifying production infrastructure and the CFO is watching).

---

## What's Inside

Enhancing existing rack (`ktw-1-s-1-r-2-5`) with:

- **+2x Dell PowerSwitch leafs and 4x L2 leafs** - More switches than the original request admitted
- **Location:** Katowice DC6, Pod 1, Suite 1, Row 2 - Where "just a small expansion" goes to retire

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
uv run infrahubctl object load data/demos/02_switch_dc6/ --branch your_branch
```

The rack generator will trigger **automatically** when the rack fabric_templates are updated! ✨

**Note:** After the rack generator completes, manually regenerate the cabling artifact:

- In InfraHub UI → Artifacts → Find "Cable matrix for DC" (DC6) → Click "Regenerate"
- Or run: `uv run infrahubctl artifact generate "Cable matrix for DC" DC6 --branch your_branch`

## Validation

Use `uv run invoke test-integration` for the complete DC lifecycle. For a quick preflight before loading expansion data, use `uv run invoke test-integration-fast`.

---

## Fun Fact

Every "temporary" rack addition is a permanent fixture by next quarter.

The only thing multiplying faster than switches is regret.

Cable management will require a PhD and a sense of humor.

The only thing more organic than this rack expansion is the panic when someone asks, "Can we add just two more switches?"

If you think this is chaotic, wait until someone suggests moving it all to the cloud.

Tip : Public cloud - because nothing says "future-proof" like paying monthly to rent someone else's chaos.
