# 03 - Single Rack Demo

The minimalist's approach (or "I ran out of budget")

## Overview

**Purpose:** Deploy a single network rack to DC6's Pod 1. Because sometimes "minimal" is just code for "we ran out of money."

**Philosophy:** Sometimes less is more. Sometimes it's just all you can afford. Sometimes it's a lie.

**Difficulty:** Trivial (until it becomes critical infrastructure and nobody wants to touch it).

---

## What's Inside

One humble rack (`ktw-1-s-1-r-4-5`) containing:

- **2x Dell PowerSwitch leafs** - Cabled to the existing DC6 Pod 1 spines
- **2x Dell PowerSwitch L2 leafs** - The bare minimum for redundancy and plausible deniability, cabled to the rack's own leafs
  and paired as a back-to-back MLAG (Pod 1 is `mlag_create: back-to-back`, like the pod's other network racks)
- **Deployment Type:** Network rack - connects to the existing DC6 Pod 1 fabric
- **Location:** Katowice DC6, Pod 1, Row 4, Index 5

---

## Use Case

This rack is perfect for:

- "We only need ONE rack" projects (that always grow)
- Budget-conscious deployments (until next quarter)
- Testing minimal viable topology (before reality sets in)
- Proof of concepts that become production (whoops)
- That application that "doesn't need much" (lies)

---

## Deployment

```bash
uv run infrahubctl branch create your_branch
uv run infrahubctl object load data/demos/03_rack_dc6/ --branch your_branch
```

The rack generator will trigger **automatically** when the rack object is created! ✨

The generator will:

1. **Create the 2 leafs** and cable them to the next free ports on the Pod 1 spines
2. **Create the 2 L2 leafs** and cable them to the rack's own leaf pair
3. **Allocate underlay ASNs and routing** for the new leafs (the L2 leafs carry no BGP), leaving every existing switch's ASN alone

**After generator completes,** manually regenerate the cabling artifact:

```bash
uv run infrahubctl artifact generate "Cable matrix for DC" DC6 --branch your_branch
```

Or in InfraHub UI → Artifacts → "Cable matrix for DC" (DC6) → Regenerate

## Validation

Run `uv run invoke test-integration` for the full rack lifecycle. If the change affects parent-to-child generator ordering, start with `uv run invoke test-integration-routing`.

---

## What Actually Happens

**Prerequisite:** DC6 Pod 1 must already exist with its parent fabric (created via the DC6 scenario).

Pod 1 is a middle_rack pod, so a network rack carries its own leafs:

1. **Rack generator runs** (automatic trigger on create — the rack is in `topologies_rack`)
2. **Resolves the pod's spines** through `pod: DC6-1-POD-1`
3. **Creates and cables the leafs** to the spines, then the L2 leafs to those leafs
4. **No spine regeneration needed** - the CablingPlanner picks the next free spine ports

Row 4 is past the 3 rows the DC6 data declares for suite `ktw-1-s-1`, but within the 4 rows the pod's M_MIDDLE
design allows.

---

## Pro Tip

This rack will outlive your tenure at the company. Plan accordingly. If you label it "test," expect it to be running in production by next year.

---

## Fun Fact

Minimal racks are never minimal for long.

The only thing more permanent than a temporary rack is a temporary workaround.
