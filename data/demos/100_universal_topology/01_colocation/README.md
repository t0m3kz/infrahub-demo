# Colocation — Someone Else's Problem, Your Invoice

> *"Colocation: the art of paying a premium to worry about your hardware
> in someone else's building instead of worrying about it in yours.
> The hardware still breaks. You just have worse WiFi when you go fix it."*

---

## Overview

**Providers:** Equinix, Coresite, Megaport | **Cities:** Amsterdam, Frankfurt, Paris
**Philosophy:** If you can't beat the cloud bill, at least own the metal

Three colocation providers, three cities, one shared conviction that cross-connects
are worth every penny until you get the invoice.

---

## What's Here

| Provider | Sites | Personality |
| --- | --- | --- |
| Equinix | AM (Amsterdam), FR (Frankfurt), PA (Paris) | The expensive one everyone uses anyway |
| Coresite | Various | The sensible backup plan |
| Megaport | AMS, FRA, PAR | Networking as a service. The irony is intentional. |

---

## Equinix — `equinix/`

The undisputed king of "your hardware, their building, their prices."
Three sites, each with racks, devices, cables, LAGs, and capability profiles
that took longer to model than the actual cross-connect provisioning.

```text
equinix/
├── am/   # Amsterdam. IBX AM1-AM11. Pick your flavour.
│         # Virtual deployment — no physical racks, because Equinix AM is a feelings-based environment.
│         # Also home to the AM4-VCG-01 virtual chassis, which sounds made up but isn't.
├── fr/   # Frankfurt. Serious racks. Serious cabling. Serious Germans.
│         # FR2 and FR6 — because one Frankfurt cage was never going to be enough.
└── pa/   # Paris. Same racks as Frankfurt but with more existential flair.
          # PA1 and PA2. The French insisted on redundancy. We respect this.
```

> **Fun fact:** Equinix AM uses a virtual deployment model in this demo because
> physical rack modeling in Amsterdam costs extra. Even in YAML.

---

## Coresite — `coresite/`

The reliable alternative. Less famous than Equinix, equally good at
not answering support tickets on bank holidays.

---

## Megaport — `megaport/`

Megaport is what happens when someone looks at colocation and thinks:
*"what if the network was also a managed service?"*
Three PoPs (AMS, FRA, PAR) providing virtual cross-connects to everything,
for when physical cables feel too committed.

```text
megaport/
├── ams/  # Amsterdam PoP. Software-defined. Very modern. Very expensive.
├── fra/  # Frankfurt PoP. See above.
└── par/  # Paris PoP. See above, but with a beret.
```

---

## A Note on Colocation Economics

An AI once analyzed our colocation spend and recommended consolidation to a single provider.
We consolidated. Then the provider had an outage. The AI had no comment.
We now have three providers. The AI has been promoted to "advisory role only."

---

*All cross-connects are modeled accurately. All cross-connect fees are not modeled,
because some things are too horrifying to put in a database.*
