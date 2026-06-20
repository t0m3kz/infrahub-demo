# DC5 - Multi-Vendor: Four Vendors, One Fabric, Infinite Support Tickets

## Overview

**Location:** New York 🇺🇸 (The city that never sleeps, neither does your infrastructure. Rack space cheaper
than a Manhattan studio!)

**Platform:** Multi-Vendor — why settle for one when you can pay four support contracts and juggle Cisco,
Arista, Dell, and Edgecore all at once?

**Fabric Design:** `M_EBGP_EBGP` — eBGP underlay + eBGP overlay (RFC 7938), IPv6 P2P links.
The only design robust enough to survive four vendors in the same fabric. eBGP everywhere means
no one can blame the IGP when things break — and with Cisco, Arista, Dell, and Edgecore all
sharing the same fabric, things will break. IPv6 underlay because at 4 pods you have enough
addresses to burn. Also because at least ONE thing should be consistent across all four vendors,
and nobody could agree on anything else.

**Use Case:**
Pod-level vendor diversity for risk mitigation and best-of-breed selection. Each pod pledges allegiance to
a different vendor—like an american version of Eurovision, but with more BGP and fewer sequins. It's
middle_rack deployment across all 4 pods because hierarchy transcends vendor boundaries (and so do support
tickets). Perfect for testing vendor migration strategies, proving your multi-vendor expertise at parties, and
discovering which vendor's CLI makes you question your life choices.

Warning: May result in spontaneous protocol debates, inter-vendor blame games, and a sudden urge to update
your LinkedIn profile.

---

## Architecture (The United Nations of Networking)

### Fabric Scale

- **Super Spines:** 2 (Cisco N9K-C9336C-FX2)
- **Pods:** 4 | **Spines:** 8 (2+2+2+2) | **Racks:** 8
- **Deployment:** `middle_rack` (all pods) - Hierarchy transcends vendor boundaries

| Pod | Spines | Spine Vendor | Design   | Deployment  | Site Layout | Personality          |
| --- | ------ | ------------ | -------- | ----------- | ----------- | -------------------- |
| 1   | 2      | Cisco        | S_MIDDLE | middle_rack | small-dc    | Enterprise Standard  |
| 2   | 2      | Arista       | S_MIDDLE | middle_rack | small-dc    | API Enthusiast       |
| 3   | 2      | Dell         | S_MIDDLE | middle_rack | small-dc    | Open Source Advocate |
| 4   | 2      | Edgecore     | S_MIDDLE | middle_rack | small-dc    | Vendor-Neutral Rebel |

## Quick Start

```bash
uv run inv deploy-dc --scenario dc5 --branch your_branch
```

**Warning:** Vendor blame games sold separately. Update your LinkedIn profile accordingly.

### ToR Layer

- **Model:** Varies by pod
- **Role:** Server connectivity

## Deployment Steps

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

Trigger infrastructure generation in InfraHub UI → Actions → Generator Definitions →
generate_dc DC5-Fabric-1

## Fun Fact

After a trip to New York and watching movies where people order "the world's best hot dog" delivered by
plane, the author bravely sampled three different vendors—just to validate the multi-vendor strategy.

Verdict: not worth the carbon footprint, and definitely not worth explaining to customs why you’re importing a hot dog.
