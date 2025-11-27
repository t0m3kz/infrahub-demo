# 04 - Pod Expansion Demo
*The "We Need More Pods" Episode*

## Overview

**Purpose:** Add Pod 4 to DC1 because Pods 1-3 weren't enough chaos. Because every good story needs a sequel.

**Philosophy:** If 3 pods are good, 4 pods are... more? Or maybe just more confusing.

**Difficulty:** Medium (high if you forget to update documentation and the compliance team is watching).

---

## What's Inside
**Pod 4 Specifications:**
- **Deployment Type:** ToR (because we already tried middle_rack and mixed in Pods 1-3)
- **Spines:** 2x Cisco N9K-C9364C-GX (the usual suspects)
- **Parent:** DC1 (the data center that keeps growing like your to-do list)
- **Philosophy:** Flat topology - keep it simple this time (famous last words)

---

## The Pod Creation Story
**Chapter 1:** "We have capacity in Pods 1-3"
**Chapter 2:** "Maybe we should isolate that new application"
**Chapter 3:** "Compliance says separate pod"
**Chapter 4:** Fine, we'll create Pod 4
**Chapter 5:** Pod 4 is now 80% utilized
**Chapter 6:** "Should we create Pod 5?" *(current location)*

---

## Use Case
Perfect for:
- **Application Isolation:** "This app needs its own pod" (translation: politics)
- **Compliance Theater:** Separate environments that share the same cooling
- **Growth Planning:** Because Pods 1-3 are somehow full already
- **Vendor Testing:** Different vendor per pod (multi-vendor chaos mode)
- **Career Development:** Adding complexity = job security

---

## Deployment
```bash
uv run infrahubctl branch create your_branch
uv run infrahubctl object load data/demos/04_pod/ --branch your_branch
```

Trigger infrastructure generation in InfraHub UI → Actions → Generator Definitions → generate_dc DC1

---

## Fun Fact

Pod 4 exists because someone said "just one more."

The only thing multiplying faster than pods is documentation debt (if you're not using Infrahub)

If you think Pod 4 is the last, you haven't met your application team.

**Welcome to pod proliferation! May your OPEX be ever in your favor.**
