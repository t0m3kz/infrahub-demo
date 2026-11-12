# 05 - Spine Expansion Demo

## Spine expansion for AI/ML workloads

## Overview

**Purpose:** Add spines to a ToR pod to increase fabric bandwidth for AI/ML (LLM) workloads.

---

## What's Inside

- **Spine Count:** 4
- **Pod:** DC1-1-POD-1
- **Upgrade Method:** Increase the pod's spine count from 2 to 4 and let the generator add the new spines

---

## Use Case

Applicable for:

- Supporting LLM and other AI/ML workloads that need more fabric bandwidth
- Increasing spine capacity of an existing pod

---

## Deployment

> This scenario is currently illustrative only. The directory contains no YAML object fixture, so the commands below describe the intended workflow rather than a loadable demo.

```bash
uv run infrahubctl branch create your_branch
# Edit DC1-1-POD-1 and increase number of Spines to 4
uv run infrahubctl object load data/demos/05_llm_time/ --branch your_branch
```

## Validation

Run `uv run invoke test-integration-routing` for generator and routing changes, or `uv run invoke test-integration` for the complete topology matrix.

The generator runs through event-driven automation, so no manual steps are needed after the load.
