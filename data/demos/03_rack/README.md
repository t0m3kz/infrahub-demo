# 03 - Single Rack Demo

The minimalist's approach (or "I ran out of budget")

## Overview

**Purpose:** Deploy a single ToR rack to DC1's Pod 2. Because sometimes "minimal" is just code for "we ran out of money."

**Philosophy:** Sometimes less is more. Sometimes it's just all you can afford. Sometimes it's a lie.

**Difficulty:** Trivial (until it becomes critical infrastructure and nobody wants to touch it).

---

## What's Inside

One humble rack (`muc-1-s-2-r-1-2`) containing:

- **2x Cisco ToRs** (N9K-C9336C-FX2) - The bare minimum for redundancy and plausible deniability
- **Deployment Type:** Mixed deployment - connects to existing middle rack leafs in row 2
- **Location:** Munich DC1, Pod 2, Row 2, Index 1

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
uv run infrahubctl object load data/demos/03_rack/ --branch your_branch
```

The rack generator will trigger **automatically** when the rack object is created! ✨

The generator will:

1. **Detect mixed deployment** and existing middle rack in row 2
2. **Automatically inherit checksum** from middle rack
3. **Generate ToR devices** immediately
4. **Connect to next available ports** on middle rack leafs

**After generator completes,** manually regenerate the cabling artifact:

```bash
uv run infrahubctl artifact generate "Cable matrix for DC" DC1 --branch your_branch
```

Or in InfraHub UI → Artifacts → "Cable matrix for DC" (DC1) → Regenerate

---

## What Actually Happens

**Prerequisite:** Pod 2 must already exist with mixed deployment (created via Scenario 01).

Pod 2's mixed deployment means:

- Some racks have leafs (middle racks at index=5 in each row)
- Some racks have only ToRs that connect to those middle rack leafs

When you add this ToR rack to row 2:

1. **ToR generator runs** (automatic trigger on create)
2. **Queries existing middle rack leafs** in row 2 (at index=5)
3. **Calculates cabling offset** based on existing ToRs in row 2
4. **Connects to next available ports** on middle rack leafs
5. **No middle rack regeneration needed** - leafs already exist with all interfaces

**Why no regeneration?** The middle rack (index=5, row 2) was created in Scenario 01 with all its leafs and interfaces. The new ToR just uses the next available ports based on its calculated offset - the CablingPlanner handles port selection automatically.

---

## How Mixed Deployment Ordering Works

The system ensures middle racks are created before ToR racks can connect to them:

Scenario 1: Middle rack exists before ToR (normal flow)

1. Middle rack creates first with leafs
2. Middle rack sets checksums on all ToR racks in same row
3. Checksum update triggers ToR rack generation
4. ToRs connect to middle rack leafs

Scenario 2: ToR added to existing middle rack (Scenario 03)

1. New ToR rack created without checksum
2. ToR generator detects mixed deployment + existing middle rack
3. **Automatically inherits checksum** from middle rack in same row
4. ToR generation proceeds immediately
5. Connects to next available ports on middle rack leafs

This dual-path mechanism ensures ToRs never generate without middle racks, regardless of creation order!

---

## Pro Tip

This rack will outlive your tenure at the company. Plan accordingly. If you label it "test," expect it to be running in production by next year.

---

## Fun Fact

Minimal racks are never minimal for long.

The only thing more permanent than a temporary rack is a temporary workaround.
