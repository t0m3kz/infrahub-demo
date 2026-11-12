# 02 - Additional Switches Demo

Add L2 leaf switches to an existing network rack.

## Overview

**Purpose:** Add switches to an existing network rack in DC6's Pod 1.

---

## What's Inside

Enhancing the existing rack (`ktw-1-s-1-r-2-5`) with:

- **L2 leafs 2 -> 4** - the rack's `l2-leaf` entry moves to a new `["4", "l2-leaf", "PowerSwitch-S5224F-ON_TOR"]` element, so
  2 more Dell PowerSwitch L2 leafs join the existing pair (the 2 leafs stay as they are)
- **Location:** Katowice DC6, Pod 1, Suite 1, Row 2

---

## Use Case

Applicable when an existing rack has spare capacity and needs additional switches, for example:

- Adding two more switches to an existing rack
- Quick expansion within the rack's power and cooling capacity

---

## Deployment

```bash
uv run infrahubctl branch create your_branch
uv run infrahubctl object load data/demos/02_switch_dc6/ --branch your_branch
```

The rack generator will trigger **automatically** when the rack fabric_templates are updated.

**Note:** After the rack generator completes, manually regenerate the cabling artifact:

- In InfraHub UI → Artifacts → Find "Cable matrix for DC" (DC6) → Click "Regenerate"
- Or run: `uv run infrahubctl artifact generate "Cable matrix for DC" DC6 --branch your_branch`

## Validation

Use `uv run invoke test-integration` for the complete DC lifecycle. For a quick preflight before loading expansion data, use `uv run invoke test-integration-fast`.
