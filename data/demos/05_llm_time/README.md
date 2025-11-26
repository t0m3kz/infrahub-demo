# 05 - Spine Expansion Demo
*The "We Need to Join the LLM Game" Episode*

## Overview
**Purpose:** Sneak extra spines into your ToR pod like a Trojan Horse—because every network wants to be LLM-ready, and every engineer wants plausible deniability.
**Philosophy:** "Just add more spines!"—the ancient battle cry of those who fear AI will take their jobs (or at least their bandwidth).
**Difficulty:** Easy (unless you have to explain to finance why you need four spines for "machine learning")

---

## What's Inside
- **Spine Count:** 4 (because two is for mortals, four is for LLM gods)
- **Pod:** DC1-1-POD-1 (the birthplace of your future AI overlords)
- **Upgrade Method:** Trojan Horse—roll in new spines under the cover of "AI readiness" and hope nobody notices until it's too late

---

## Use Case
Perfect for:
- "We need to support LLM workloads" (translation: we want to sound cool at conferences)
- "AI-ready infrastructure" (translation: we have no idea what we're doing, but it looks good on slides)
- "Future-proofing" (translation: we want to buy more hardware before the budget disappears)
- "Just in case ChatGPT wants to run on-prem" (translation: we fear the cloud)

---

## Deployment
```bash
# Edit DC1-1-POD-1 and increase number of Spines to 4
uv run infrahubctl object load data/demos/05_llm_time/
```

---

## Fun Fact
- The only thing more mysterious than LLMs is why you suddenly need four spines.
- Trojan Horse upgrades: because sometimes the best way to get what you want is to hide it in a box labeled "AI".
- If your spines start writing poetry, it's time to panic.