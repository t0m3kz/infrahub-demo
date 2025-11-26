# DC5 - Multi-Vendor: Eurovision for Switches

## Overview
**Location:** New York 🇺🇸 (The city that never sleeps, neither does your infrastructure. Rack space cheaper than a Manhattan studio!)

**Size:** Medium (M) - Diverse, resilient, and a little bit chaotic

**Platform:** Multi-Vendor (Cisco, Arista, Dell SONiC, Edgecore SONiC)

**Design Pattern:** M-Multi-Pod-MR (Medium Multi-Vendor Per Pod, Middle Rack)

**Use Case:**
Pod-level vendor diversity for risk mitigation and best-of-breed selection. Each pod pledges allegiance to a different vendor—like a networking version of Eurovision, but with more BGP and fewer sequins. It's middle_rack deployment across all 4 pods because hierarchy transcends vendor boundaries (and so do support tickets). Perfect for testing vendor migration strategies, proving your multi-vendor expertise at parties, and discovering which vendor's CLI makes you question your life choices. Warning: May result in spontaneous protocol debates, inter-vendor blame games, and a sudden urge to update your LinkedIn profile.

---

## Architecture (The United Nations of Networking)

### Fabric Scale
- **Super Spines:** 2 (Cisco N9K-C9336C-FX2)
- **Total Pods:** 4 (One vendor per pod)
- **Total Spines:** 8 (2 per pod)
- **Total Racks:** 8 (2 per pod)
- **Deployment Type:** middle_rack (all pods)

### Pod Structure (Vendor Diversity Initiative)
| Pod   | Spines | Vendor   | Model                | Racks | Deployment   | Personality                |
|-------|--------|----------|----------------------|-------|-------------|----------------------------|
| Pod 1 | 2      | Cisco    | N9K-C9364C-GX        | 2     | middle_rack | The Enterprise Standard    |
| Pod 2 | 2      | Arista   | DCS-7050CX3-32C-R    | 2     | middle_rack | The API Enthusiast         |
| Pod 3 | 2      | Dell     | S5232F-ON            | 2     | middle_rack | The Open Source Advocate   |
| Pod 4 | 2      | Edgecore | 7726-32X-O           | 2     | middle_rack | The Vendor-Neutral Rebel   |

---

## Hardware Stack (Vendor Diversity Champions)

### Super Spine Layer
- **Model:** Cisco N9K-C9336C-FX2
- **Ports:** 36x100GbE
- **Role:** Inter-pod connectivity
- **Fun Fact:** Top of the food chain

### Spine Layer
- **Model:** Varies by pod (see table above)
- **Ports:** 32-36x100GbE
- **Role:** Pod-level aggregation

### Leaf Layer
- **Model:** Varies by pod
- **Role:** Rack-level aggregation

### ToR Layer
- **Model:** Varies by pod
- **Role:** Server connectivity

-

## Quick Start

```bash
# really quick
uv run inv deploy-dc --scenario dc5 --branch your_branch

# I'm the control nerd
uv run infrahubctl branch create you_branch

# Load topology (this is the point of no return)
uv run infrahubctl object load data/demos/01_data_center/dc5/ --branch you_branch

# Generate fabric (grab coffee, this might take a while)
uv run infrahubctl generator generate_dc name=DC5 --branch you_branch

```

Trigger infrastructure generation in InfraHub UI → Actions → Generator Definitions → generate_dc DC5-Fabric-1

## Fun Fact
After visit in
