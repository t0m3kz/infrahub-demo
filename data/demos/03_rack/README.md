# 03 - Single Rack Demo
*The Minimalist's Approach (Or "I Ran Out of Budget")*

## Overview
**Purpose:** Deploy a single ToR rack to DC1's Pod 2. Because sometimes "minimal" is just code for "we ran out of money."

**Philosophy:** Sometimes less is more. Sometimes it's just all you can afford. Sometimes it's a lie.

**Difficulty:** Trivial (until it becomes critical infrastructure and nobody wants to touch it).

---

## What's Inside
One humble rack (`muc-1-s-2-r-1-2`) containing:
- **2x Cisco ToRs** (N9K-C9336C-FX2) - The bare minimum for redundancy and plausible deniability
- **Deployment Type:** Pure ToR (flat topology) - No middle management here, just existential dread
- **Location:** Munich DC1, Pod 2, Row 1, Position 2

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
uv run infrahubctl object load data/demos/03_rack/
```

---

## Pro Tip
This rack will outlive your tenure at the company. Plan accordingly. If you label it "test," expect it to be running in production by next year.

---

## Fun Fact

inimal racks are never minimal for long.

The only thing more permanent than a temporary rack is a temporary workaround.
