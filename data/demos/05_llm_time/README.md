# 05 - Spine Expansion Demo

## The "We Need to Join the LLM Game" Episode

## Overview

**Purpose:** Sneak extra spines into your ToR pod like a Trojan Horse, because every network wants to be
LLM-ready, and every engineer wants plausible deniability. Budget approval optional, buzzwords mandatory.

**Philosophy:** "Just add more spines!" — the ancient battle cry of those who fear AI will take their jobs (or at
least their bandwidth). It has never once been wrong, which is exactly what makes it dangerous.

**Difficulty:** Easy (unless you have to explain to finance why you need four spines for "machine
learning", in which case, good luck)

---

## What's Inside

- **Spine Count:** 4 (because two is for mortals, four is for LLM gods)
- **Pod:** DC1-1-POD-1 (the birthplace of your future AI overlords, which is a lot of pressure for a pod)
- **Upgrade Method:** Trojan Horse — roll in new spines under the cover of "AI readiness" and hope nobody
  notices until it's too late (the invoice will notice)

---

## Use Case

Perfect for:

- "We need to support LLM workloads" (translation: we want to sound cool at conferences)
- "AI-ready infrastructure" (translation: we have no idea what we're doing, but it looks excellent on slides)
- "Future-proofing" (translation: we want to buy more hardware before the budget disappears)
- "Just in case ChatGPT wants to run on-prem" (translation: we fear the cloud, and the cloud knows)

---

## Deployment

> This scenario is currently illustrative only. The directory contains no YAML object fixture, so the commands below describe the intended workflow rather than a loadable demo. Yes, it is a demo of a demo that cannot be loaded; it is that kind of episode.

```bash
uv run infrahubctl branch create your_branch
# Edit DC1-1-POD-3 and increase number of Spines to 4
uv run infrahubctl object load data/demos/05_llm_time/ --branch your_branch
```

## Validation

Run `uv run invoke test-integration-routing` for generator and routing changes, or `uv run invoke test-integration` for the complete topology matrix. The spines may be for AI, but the test runner still prefers evidence, which is more than can be said for the slide deck.

Here you just sit back, sip your coffee, and watch the magic happen: no clicking, no frantic tab switching,
just pure event-driven automation (tip: do not touch anything, it is more nervous than you are).

It's like Netflix for network engineers, but with fewer cliffhangers and no "are you still watching?" prompt.

---

## Fun Fact

The only thing more mysterious than LLMs is why you suddenly need four spines. Ask finance. Actually, don't.

Trojan Horse upgrades: because sometimes the best way to get what you want is to hide it in a box labeled "AI".

If your spines start writing poetry, it's time to panic.

Tip: Public cloud - for when you want your AI to be someone else's outage problem.
