# POP - Point of Presence: Colocation Chaos

Where Your Infrastructure Meets Other People's Infrastructure (And Everyone Pretends It's Fine)

## Overview

**Providers:** Equinix 🌐 (the landlord of the internet), Megaport 🔌 (the landlord of the wires between the landlords), CoreSite 🏢 (the American cousin)

**Location Strategy:** Multi-provider, two continents — Frankfurt 🇩🇪, Paris 🇫🇷, Amsterdam 🇳🇱, New York, Virginia, Silicon Valley

**Architecture:** Colocation POP — because why own a data center when you can rent tiny rooms in someone else's?

**Use Case:** Point of Presence deployment for companies who want global reach without building their own
infrastructure. Perfect for demonstrating how to turn monthly colocation bills into quarterly budget meetings, and
why "just a few racks" never stays "just a few racks."

This demo stands alone: it loads no data centers, no clouds, and no customers. If you want the on-ramp with
something behind it, load `data/demos/30_all/` instead.

---

## Architecture (Expensive Real Estate Strategy)

### Footprint

| | Count |
| ------------------ | ----- |
| Providers | 3 |
| Metros | 9 |
| Cages (AZs) | 14 |
| Racks | 11 |
| Generated devices | 8 |

### Metro Structure

| Provider | Metro | Location | Cages | Racks | On-ramp kit |
| ---------- | ------- | ---------------- | ------------------- | ----- | ------------------------- |
| Equinix | FR | Frankfurt 🇩🇪 | FR2, FR6 | 4 | 2 × edge + 2 × firewall |
| Equinix | PA | Paris 🇫🇷 | PA4, PA6 | 4 | 2 × edge |
| Equinix | AM | Amsterdam 🇳🇱 | AM1, AM4 | — | 2 × edge (virtual) |
| Megaport | AMS | Amsterdam 🇳🇱 | MCR-AMS1 | 1 | — |
| Megaport | FRA | Frankfurt 🇩🇪 | MCR-FRA1 | 1 | — |
| Megaport | PAR | Paris 🇫🇷 | MCR-PAR1 | 1 | — |
| CoreSite | NY | New York | NY1, NY2 | — | — |
| CoreSite | VA | Virginia | VA1, VA2 | — | — |
| CoreSite | SV | Silicon Valley | SV1 | — | — |

---

## The On-Ramp Is Generated, Not Written

No on-ramp router appears in these files as a device. Each metro **declares** what it wants and
`add_colocation_metro` (`generators/topology/colocation.py`) builds it:

```yaml
- name: FR
  location: ["fra"]
  member_of_groups: [colocation_metros]
  fabric_templates:
    - ["2", "edge", "N9K-C9316D-GX_EDGE"]
    - ["2", "firewall", "PA-5260_FIREWALL"]
```

From that the generator creates, per metro:

- the metro's own **loopback / management / technical / ASN pools**, sliced out of the global bootstrap supernets
- the **devices** — `eg-fr01`, `eg-fr02`, `fw-fr01`, `fw-fr02` — each with a management address, and a /32 loopback
  for the edge routers only
- an **HA domain** for a firewall or load-balancer pair (`ManagedFirewallHA`), its two `ManagedHAInterface` nodes,
  and the sync cable between the members

The kit is declared on the **metro**, not on a cage, for the same reason a data center declares its border-leafs
rather than one of its pods doing it: one pair fronts the whole metro, and the other cages reach it over a
cross-connect.

### Physical vs virtual

A metro's `deployment_type` gates what its `fabric_templates` may name:

| `deployment_type` | Means | Templates allowed |
| ------------------- | ------------------------------------------- | ------------------------- |
| `physical` (default) | our own hardware in cage space we rent | physical only |
| `virtual` | a provider-hosted instance we rent | virtual only |
| `hybrid` | both | either |

Frankfurt and Paris are `physical` and name an N9K. Amsterdam is `virtual` and names `C8000V_EDGE`, so it gets
`DcimVirtualDevice`s — an Equinix Network Edge instance, not hardware — and a physical template there would be
rejected.

### Metros with no kit

Megaport's MCRs and all three CoreSite metros declare no `fabric_templates`. That is deliberate, not an omission:
an MCR is Megaport's own virtual router in Megaport's cloud (we buy a port on it, we do not deploy into it), and the
CoreSite metros are cage space we rent without running an on-ramp. They still join `colocation_metros`, because the
`created` trigger fires per kind and Infrahub fails a generator run whose target sits outside the definition's
group — so the generator runs and no-ops.

---

## Quick Start (For the Financially Brave)

```bash
# Create a branch — never load demo data straight into main
uv run infrahubctl branch create pop_deployment

# Load the colocation structure (bootstrap must already be loaded)
uv run infrahubctl object load data/demos/10_colocation/ --branch pop_deployment
```

The generator fires automatically on each metro as it is created — no manual run needed. When the tasks finish the
branch holds eight devices that are in none of these files: `eg-fr01`, `eg-fr02`, `fw-fr01`, `fw-fr02`, `eg-pa01`,
`eg-pa02`, `eg-am01`, `eg-am02`.

To re-run one metro by hand (idempotent — same devices, same addresses):

```bash
uv run infrahubctl generator add_colocation_metro name=FR --branch pop_deployment
```

## Validation

Use `uv run invoke test-integration-fast` to verify shared setup and repository prerequisites before exercising a
colocation branch manually. Use `uv run invoke test-integration` for the full integration matrix. Note that the
integration suite loads `data/demos/30_all/`, not this demo — changes here are verified by loading the branch.

## Fun Facts

### Frankfurt Edition

- DE-CIX Frankfurt is one of the world's largest internet exchanges by data throughput
- More internet traffic flows through Frankfurt than through most small countries' entire economies
- German data centers are so efficient, even the backup generators are engineered to perfection
- Local beer is excellent, but don't drink before making configuration changes

### Paris Edition

- France has some of the best nuclear-powered data centers in the world
- French internet infrastructure is surprisingly robust (like their cheese and wine)
- Parisians take lunch seriously, even in data centers
- The pastry shops near Equinix Paris facilities are legendary among network engineers

### Amsterdam Edition

- AMS-IX is the other giant exchange everyone compares DE-CIX to, usually while standing in Frankfurt
- Nothing in this metro is ours: the routers are rented instances in someone else's cloud
- The cages hold no racks, which makes the capacity planning meetings refreshingly short
