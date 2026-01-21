# DC5 - Multi-Vendor: Four Vendors, One Fabric, Infinite Support Tickets

## Overview

**Location:** New York 🇺🇸 (The city that never sleeps, neither does your infrastructure. Rack space cheaper than a Manhattan studio!)

**Platform:** Multi-Vendor — why settle for one when you can pay four support contracts and juggle Cisco, Arista, Dell, and Edgecore all at once?

**Fabric Design:** `ebgp-ipv4-large` - Large-scale eBGP for 4 pods (when standard isn't big enough)

**Use Case:**
Pod-level vendor diversity for risk mitigation and best-of-breed selection. Each pod pledges allegiance to a different vendor—like a american version of Eurovision, but with more BGP and fewer sequins. It's middle_rack deployment across all 4 pods because hierarchy transcends vendor boundaries (and so do support tickets). Perfect for testing vendor migration strategies, proving your multi-vendor expertise at parties, and discovering which vendor's CLI makes you question your life choices.

Warning: May result in spontaneous protocol debates, inter-vendor blame games, and a sudden urge to update your LinkedIn profile.

---

## Architecture (The United Nations of Networking)

### Fabric Scale

- **Super Spines:** 2 (Cisco N9K-C9336C-FX2)
- **Pods:** 4 | **Spines:** 16 (4+4+4+4) | **Racks:** 8
- **Deployment:** `middle_rack` (all pods) - Hierarchy transcends vendor boundaries

| Pod | Spines | Vendor   | Design                       | Site Layout | Personality          |
| --- | ------ | -------- | ---------------------------- | ----------- | -------------------- |
| 1   | 4      | Cisco    | spine-leaf-middlerack-4spine | small-dc    | Enterprise Standard  |
| 2   | 4      | Arista   | spine-leaf-middlerack-4spine | small-dc    | API Enthusiast       |
| 3   | 4      | Dell     | spine-leaf-middlerack-4spine | small-dc    | Open Source Advocate |
| 4   | 4      | Edgecore | spine-leaf-middlerack-4spine | small-dc    | Vendor-Neutral Rebel |

## Quick Start

```bash
uv run inv deploy-dc --scenario dc5 --branch your_branch
```

**Warning:** Vendor blame games sold separately. Update your LinkedIn profile accordingly

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

After a trip to New York and watching movies where people order “the world’s best hot dog” delivered by plane, the author bravely sampled three different vendors—just to validate the multi-vendor strategy.

Verdict: not worth the carbon footprint, and definitely not worth explaining to customs why you’re importing a hot dog.