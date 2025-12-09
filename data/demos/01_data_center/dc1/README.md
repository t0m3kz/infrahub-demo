# DC1 - Hierarchy Overkill: The Kitchen Sink Data Center

## Overview

**Location:** Munich 🇩🇪 (Home to Oktoberfest and BMW - where beer gardens meet precision engineering)

**Size:** Large (L) - Like your cloud bill after forgetting to turn off dev instances

**Platform:** Cisco Nexus 9K - The networking equivalent of driving a tank to the grocery store

**Design Pattern:** L-Hierarchical (Large with Hierarchical naming convention)

**Use Case:** Enterprise data center suffering from **deployment identity crisis** - featuring all middle rack, mixed and ToR connectivity within the same fabric. This is what happens when the architecture committee couldn't decide, so they chose "all of the above." Perfect for demonstrating that flexibility isn't always a feature; sometimes it's just indecision with a marketing spin.

---

## Architecture (AKA: The Magnificent Mess)

### Fabric Scale (Or: How We Learned to Stop Worrying and Love Complexity)

- **Super Spines:** 2 (Cisco N9K-C9336C-FX2) - The bosses of bosses
- **Total Pods:** 3 (Each with its own personality disorder)
- **Total Spines:** 8 (3+3+2 - we support diversity and inclusiveness)
- **Total Racks:** 24 (Because stopping at 20 would suggest we had a plan)

**Deployment Types:** It’s complicated—Pod 1: middle_rack (for fans of bureaucracy), Pod 2: mixed (for the indecisive), Pod 3: ToR (for rebels who read “Keep It Simple” and actually believed it).

### Pod Structure (The Family Dysfunction Table)

| Pod | Spines | Deployment Type | Racks | Personality |
|-----|--------|----------------|-------|-------------|
| POD-1 | 3 | middle_rack | 4 middle racks  | The overachiever with hierarchy complex |
| POD-2 | 3 | mixed | 8 mixed (4 middle + 4 ToR) | The confused middle child trying both strategies |
| POD-3 | 2 | tor | 12 ToR racks | The minimalist who read "Keep It Simple" once and took it seriously |

### Design Template Constraints (Or: The Rules We Pretend to Follow)

- maximum_super_spines: 4 (but we only use 2 because who needs redundancy, right?)
- maximum_spines: 4 per pod (democracy in action)
- maximum_pods: 4 (we're only using 3 - always leave room for "future growth")
- maximum_leafs: 24 (enough to make your monitoring dashboard look like a Christmas tree)
- maximum_rack_leafs: 8 (per rack, because why keep it simple?)
- maximum_middle_racks: 8 (bureaucracy loves middle management)
- maximum_tors: 48 (that's a lot of Top-of-Racks, or as we call them, "spine port consumers")
- naming_convention: hierarchical (because `device_42` was too obvious)

---

## Hardware Stack (The Expensive Bits)

### Super Spine Layer (The Executive Suite)

- **Model:** Cisco N9K-C9336C-FX2
- **Ports:** 36x100GbE (fewer ports, higher paygrade)
- **Role:** Inter-pod connectivity and being generally superior
- **Fun Fact:** Only talks to spines, has security escort VLANs

### Spine Layer (The Middle Management)

- **Model:** Cisco N9K-C9364C-GX
- **Ports:** 64x100GbE (that's a lot of cables to accidentally unplug)
- **Role:** Pod-level aggregation and professional packet shuffler
- **Fun Fact:** Each port costs more than your car payment

### Leaf Layer (The Worker Bees - Middle Rack Edition)

- **Model:** Cisco N9K-C9336C-FX2
- **Ports:** 36x100GbE (proving that middle management can have nice things too)
- **Role:** Rack-level aggregation in Pods 1-2
- **Fun Fact:** Gets to boss ToRs around while taking orders from spines

### ToR Layer (The Actual Workers)

- **Model:** Various (because vendor diversity is a virtue when you can't decide)
- **Deployment:** Direct spine connection (Pod 3) or leaf connection (Pods 1-2)
- **Role:** Connecting servers and pretending not to resent the hierarchy above
- **Fun Fact:** Closest to the actual servers, knows where the bodies are buried

---

## Deployment Strategy (Choose Your Own Adventure)

### Middle Rack Deployment (Pod 1: The Bureaucratic Approach)

**Philosophy:** "Why connect directly when you can add another layer?"

**ToR Connectivity:**

- ToRs connect to local Leafs in racks (because chain of command matters)
- If no local Leafs exist, connection attempts external Leafs (the backup plan nobody tested)
- Reduces spine port consumption (saving ports for "future growth" that never comes)
- Better for hierarchical aggregation (and org charts that look impressive)
- **Latency:** Slightly higher, but you get bragging rights about "architecture"
- **Complexity:** Maximum (job security through obscurity)

### Mixed Deployment (Pod 2: The "Best of Both Worlds" Disaster)

**Philosophy:** "Can't we all just get along?"

- Some racks with middle leafs (bureaucracy lovers)
- Some racks with direct ToR-to-spine (pragmatists)
- Demonstrates flexibility (or inability to commit)
- Perfect for confusing your monitoring team
- **Latency:** Depends on which path packet took and whether it filed proper paperwork
- **Complexity:** Yes

### ToR Deployment (Pod 3: The Rebel Alliance)

**Philosophy:** "Screw the hierarchy, let's just make it work"

**ToR Connectivity:**

- ToRs connect directly to Spines (radical concept: skip middle management)
- Simpler, flatter topology (fewer things to break at 3 AM)
- Lower latency (packets don't need org chart)
- Better for east-west traffic (which is most of your traffic anyway)
- **Latency:** Lower (physics still works)
- **Complexity:** Minimal (but where's the fun in that?)

---

## Quick Start (For the Brave)

```bash
# really quick
uv run inv deploy-dc --scenario dc1 --branch your_branch

# I'm the control nerd
uv run infrahubctl branch create you_branch

# Load topology (this is the point of no return)
uv run infrahubctl object load data/demos/01_data_center/dc1/ --branch you_branch

# Generate fabric (grab coffee, this might take a while)
uv run infrahubctl generator generate_dc name=DC1 --branch you_branch

```

Trigger infrastructure generation in InfraHub UI → Actions → Generator Definitions → generate_dc DC1-Fabric-1

Watch the magic happen (or the chaos unfold, depending on your perspective)

Pro tip: Have the InfraHub UI open to see devices spawn like rabbits

Create a Proposed Change (PC) and watch the chaos unfold in real time.

## Fun Fact

The author lives in Munich and has spent years trying to understand the rules of Schafkopf as explained in Bavarian—proof that some network topologies are easier to decipher than local card games.

If you ever win a round, you’re officially more Bavarian than a pretzel at Oktoberfest.

Munich Fact: The city’s official symbol is the Münchner Kindl—a child in a monk’s robe, which is still less mysterious than Bavarian pronunciation (especially after your second Maß of beer).
