# Interconnects — The Duct Tape of Enterprise Networking

> *"We asked an AI to map our interconnect dependencies.
> It drew a circle, labeled it 'Equinix,' and drew lines from everything to it.
> It was the most accurate network diagram we have ever produced."*

---

## Overview

**Type:** Cross-connects, dark fibre, virtual circuits, cables
**Volume:** More than you want to count
**Equinix involvement:** Mandatory, apparently

The interconnects layer is where everything connects to everything else,
usually via Amsterdam, Frankfurt, or Paris, and almost certainly via Equinix.

---

## What's Here

```text
05_interconnects/
├── 06_provider_devices.yml     # Provider routers and patch panels. The physical reality.
├── 07_cables.yml               # The actual cables. Numbered. Labeled. Occasionally argued about.
├── zz_fabric-peering-bgp.yml   # BGP peering across the fabric. Loads last. Named accordingly.
│
├── cloud-equinix/              # Cloud ↔ Equinix. AWS/Azure via Direct Connect / ExpressRoute.
│                               # Expensive. Fast. Non-negotiable.
├── datacenter-equinix/         # DC1/DC2 ↔ Equinix. Cross-connects. The physical handoff.
├── equinix-equinix/            # Equinix ↔ Equinix. Because sometimes you need to go from
│                               # one cage to another cage in the same building via fiber.
│                               # Equinix charges for this. Of course it does.
├── equinix-internet/           # Equinix ↔ ISPs. Where your traffic meets the world.
├── equinix-partners/           # Equinix ↔ Partners. Cross-connects to ACME and Globex.
│                               # The fiber of business relationships.
└── offices-equinix/            # Offices ↔ Equinix. MPLS and SD-WAN backhaul.
                                # Five offices. One hub. All roads lead to Amsterdam.
```

---

## The Equinix Problem

If you look at this topology as a graph, you will notice that Equinix appears
in approximately every path between any two endpoints. This is not a design flaw.
This is the natural state of European enterprise interconnect architecture.

Equinix AM, FR, and PA are the three load-bearing nodes of this entire topology.
If they go down simultaneously, we have bigger problems than this README can address.

> *Fun fact: "equinix-equinix" is a real category of cross-connect.
> You pay Equinix to connect your cage to another cage inside the same Equinix building.
> An AI was asked if this was economically rational. It said "it depends on your use case."
> That's a yes.*

---

## Load Order Note

The file `zz_fabric-peering-bgp.yml` starts with `zz_` because it must load last.
This is not a naming convention we're proud of. It is a naming convention that works.
Sometimes those are the same thing. This time they are not.

---

## BGP: A Brief Meditation

Somewhere in `06_peering-bgp.yml` and `zz_fabric-peering-bgp.yml` are BGP sessions
that connect this entire topology into a coherent routing domain. Each session has
a remote AS, a password that will be rotated "soon," and a timer configuration
that was tuned once in 2021 and has not been questioned since.

An AI reviewed the BGP configuration and described it as "stable and conservative."
We accepted this as a compliment.

---

*All interconnects are modeled. All interconnect fees are not modeled.
The fiber is real. The cross-connect invoices would make you cry.
We have chosen not to model things that make people cry.*
