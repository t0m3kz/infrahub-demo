# DC4 - Mixed Chaos: Can't Decide? Deploy Both!

## Overview

**Location:** Berlin 🇩🇪 | **Size:** Small | **Platform:** Edgecore SONiC | **Design:** Mixed bag

When the architecture team can't agree and someone says "why not both?" Pod 1 goes `mixed`, Pod 2 goes flat `tor`. It's the networking equivalent of a hipster café menu—confused but oddly functional.

**Use Case:** Flexibility through indecision. Now with mandatory 4 spines per pod!

## Architecture

- **Super Spines:** 2 (Edgecore 7726-32X-O)
- **Pods:** 2 | **Spines:** 8 (4+4) | **Racks:** 5
- **Deployment:** `mixed` (Pod 1), `tor` (Pod 2) - Because commitment is overrated

| Pod | Spines | Design                  | Site Layout | Personality  |
| --- | ------ | ----------------------- | ----------- | ------------ |
| 1   | 4      | spine-leaf-mixed-4spine | small-dc    | Overachiever |
| 2   | 4      | spine-leaf-tor-4spine   | small-dc    | Minimalist   |

## Quick Start

```bash
uv run inv deploy-dc --scenario dc4 --branch your_branch
```

**Warning:** May cause identity crisis. Perfect for flexing multi-deployment skills

## Quick Start

```bash
# really quick
uv run inv deploy-dc --scenario dc4 --branch your_branch

# I'm the control nerd
uv run infrahubctl branch create you_branch

# Load topology (this is the point of no return)
uv run infrahubctl object load data/demos/01_data_center/dc4/ --branch you_branch

# Generate fabric (grab coffee, this might take a while)
uv run infrahubctl generator generate_dc name=DC4 --branch you_branch

```

Trigger infrastructure generation in InfraHub UI → Actions → Generator Definitions → generate_dc DC4-Fabric-1

## Fun Fact

The author owns a piece of the Berlin Wall—so if your network ever feels divided, just remember: it can be rebuilt, repurposed, or turned into a conversation starter at tech meetups. It’s a daily reminder that even the toughest partitions eventually fall—sometimes with a little help from automation, sometimes with a sledgehammer.

Bonus: The author proudly benefits from Germany’s Unity Day, enjoying a free holiday every year thanks to history and a chunk of concrete.

Prost to open borders, open networks, and open source!