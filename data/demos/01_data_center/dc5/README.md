# DC5 - Multi-Vendor: Four Vendors, One Fabric, Infinite Support Tickets

## Overview

**Location:** New York 🇺🇸 (The city that never sleeps, and after a week of on-call, neither will you. Rack space is still cheaper
than a Manhattan studio, which says more about Manhattan than about the rack space.)

**Platform:** Multi-Vendor — because someone in procurement looked at one support contract and thought "that is clearly too relaxing", so now you get to juggle
Cisco, Arista, Dell, and Edgecore all at once.

**Fabric Design:** `L` (small instance) + `ebgp-ebgp` routing — eBGP underlay + eBGP overlay (RFC 7938), IPv6 P2P links.
It is the only design sturdy enough to survive four vendors in the same fabric. eBGP everywhere means
nobody gets to blame the IGP when things break, and with Cisco, Arista, Dell, and Edgecore all
sharing one fabric, things will break, so at least the blame has to be creative. The underlay is IPv6 because at 4 pods you have enough
addresses to burn. It is also IPv6 because at least ONE thing should be the same across all four vendors,
and nobody could agree on anything else.

**Use Case:**
Pod-level vendor diversity for risk mitigation and best-of-breed selection (the brochure says "best-of-breed", the pager says "worst-of-four"). Each pod pledges allegiance to
a different vendor, like an American Eurovision with more BGP and fewer sequins. It is
middle_rack deployment across all 4 pods, because hierarchy transcends vendor boundaries (and so do support
tickets). Perfect for testing vendor migration strategies, proving your multi-vendor expertise at parties (nobody asked), and
finding out which vendor's CLI makes you question your life choices.

Warning: may result in spontaneous protocol debates, inter-vendor blame games, and a sudden urge to update
your LinkedIn profile. Yes, really.

---

## Architecture (The United Nations of Networking)

### Fabric Scale

- **Super Spines:** 2 (Cisco N9K-C9336C-FX2), the only part of this build where everyone agrees on the vendor
- **Pods:** 4 | **Spines:** 8 (2+2+2+2) | **Racks:** 8
- **Deployment:** `middle_rack` (all pods) - Hierarchy transcends vendor boundaries, unlike your config syntax

| Pod | Spines | Spine Vendor | Design   | Deployment  | Site Layout | Personality          |
| --- | ------ | ------------ | -------- | ----------- | ----------- | -------------------- |
| 1   | 2      | Cisco        | S_MIDDLE | middle_rack | small-dc    | Enterprise Standard  |
| 2   | 2      | Arista       | S_MIDDLE | middle_rack | small-dc    | API Enthusiast       |
| 3   | 2      | Dell         | S_MIDDLE | middle_rack | small-dc    | Open Source Advocate |
| 4   | 2      | Edgecore     | S_MIDDLE | middle_rack | small-dc    | Vendor-Neutral Rebel |

## Quick Start

```bash
# See Deployment Steps below for the current load and generator commands.
```

**Warning:** Vendor blame games sold separately. Update your LinkedIn profile accordingly, preferably before the first outage.

### ToR Layer

- **Model:** Varies by pod, because uniformity would have been far too easy
- **Role:** Server connectivity (the part the business actually cares about)

## Deployment Steps

```bash
# really quick
# See the manual commands below, or use the automatic created-object trigger.

# I'm the control nerd
uv run infrahubctl branch create you_branch

# Load topology (this is the point of no return)
uv run infrahubctl object load data/demos/01_data_center/dc5/ --branch you_branch

# Generate fabric (grab coffee, this might take a while)
uv run infrahubctl generator add_dc name=DC5 --branch you_branch

```

Or skip the terminal and trigger infrastructure generation in InfraHub UI → Actions → Generator Definitions →
add_dc for DC5. Same result, more clicking, a mouse for company.

## Validation

Use `uv run invoke test-integration` for the full multi-vendor matrix. For automatic DC/POD/rack ordering changes, start with `uv run invoke test-integration-routing`; Eurovision for switches deserves at least one rehearsal, and so does anything with four vendors.

## Fun Fact

After a trip to New York and watching movies where people order "the world's best hot dog" delivered by
plane, the author bravely sampled three different vendors, purely to validate the multi-vendor strategy (and because the hot dog could not be shipped).

Verdict: not worth the carbon footprint, and definitely not worth explaining to customs why you’re importing a hot dog.
