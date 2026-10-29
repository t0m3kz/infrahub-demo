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
| ------------------------ | ----- |
| Providers | 3 |
| Metros | 9 |
| Cages (zones) | 12 |
| Virtual colocations | 1 |
| Racks | 11 |
| Generated devices | 8 |
| Generated cables | 5 |

### Metro Structure

A metro is the Multi-Site site: it is no facility and owns no devices. Its children are the places that hold kit,
**zones** (our own hardware in a cage) and **virtual colocations** (a provider-hosted presence, nothing racked).

| Provider | Metro | Location | Zones / virtual colocation | Racks | On-ramp kit (declared on) |
| ---------- | ------- | ---------------- | -------------------------- | ----- | ------------------------------------------ |
| Equinix | FR | Frankfurt 🇩🇪 | FR2, FR6 | 4 | FR2: 2 × edge + 2 × firewall |
| Equinix | PA | Paris 🇫🇷 | PA4, PA6 | 4 | PA4: 2 × edge |
| Equinix | AM | Amsterdam 🇳🇱 | AM-NE (virtual colocation) | — | AM-NE: 2 × edge (virtual) |
| Megaport | AMS | Amsterdam 🇳🇱 | MCR-AMS1 | 1 | — |
| Megaport | FRA | Frankfurt 🇩🇪 | MCR-FRA1 | 1 | — |
| Megaport | PAR | Paris 🇫🇷 | MCR-PAR1 | 1 | — |
| CoreSite | NY | New York | NY1, NY2 | — | — |
| CoreSite | VA | Virginia | VA1, VA2 | — | — |
| CoreSite | SV | Silicon Valley | SV1 | — | — |

---

## The On-Ramp Is Generated, Not Written

No on-ramp router appears in these files as a device. Each **zone** (or virtual colocation) **declares** what it
wants and `add_colocation_zone` (or `add_virtual_colocation`), both in `generators/topology/colocation_fabric.py`,
builds it:

```yaml
- name: FR2
  member_of_groups: [colocation_zones]
  fabric_templates:
    - ["2", "edge", "N9K-C9316D-GX_EDGE"]
    - ["2", "firewall", "PA-5260_FIREWALL"]
```

From that the generator creates, per zone:

- the zone's own **loopback / management / technical pools**, sliced out of the global bootstrap supernets (the
  metro keeps only the VNI and ASN pools, built by `add_colocation_metro`, and the one EVPN site AS drawn from the
  metro's ASN pool)
- the **devices** — `eg-fr201`, `eg-fr202`, `fw-fr201`, `fw-fr202` — named after the zone, each with a management
  address, and a /32 loopback for the edge routers only
- an **HA domain** for a firewall or load-balancer pair (`ManagedFirewallHA`) and the sync cable between the members
- the **cabling** from the edge pair to that service pair, on the edge's `firewall`-role ports
  (`Ethernet1/[15-16]`) and the firewall's `uplink` ports
- for a zone with exactly two physical edges, the **edge pair link**: a cable on the edge's `interlink`-role port
  (`Ethernet1/8`) with a /127 from the zone's own `technical-ipv6` pool, and an iBGP `FABRIC-eg-fr201-eg-fr202`
  session between their overlay processes

The kit is declared on the **zone** that physically holds it, not on the metro and not on every cage: for the same
reason a data center declares its border-leafs rather than one of its pods doing it, one pair fronts the whole metro
and the other cages (FR6) reach it over a cross-connect. FR6 and PA6 declare nothing.

### What gets cabled, and what does not

Frankfurt is the only zone here with two tiers to join, and it ends up with five cables:

```text
eg-fr201:Ethernet1/15 <-> fw-fr201:ethernet1/1     eg-fr202:Ethernet1/15 <-> fw-fr202:ethernet1/1
eg-fr201:Ethernet1/16 <-> fw-fr201:ethernet1/2     eg-fr202:Ethernet1/16 <-> fw-fr202:ethernet1/2
                        fw-fr201:HA1 <-> fw-fr202:HA1
```

Index-paired, never any-to-any: `eg-fr201` reaches only `fw-fr201`, so each edge/firewall couple is one independent
redundant path. This is the same leg — and the same helper — a data center uses between its border-leafs and its
shared services, because it is the same shape: the appliance is not in the underlay, it hangs off the tier that is.

A `DcimCable` means fibre someone pulled between two chassis, so the generator lays one only where both ends are
**our own hardware** in **one zone**. That is the whole rule, and it is what keeps two other kinds of link out:

**Links that leave the zone.** The on-ramp's links to the DC border-leafs (the edge's `dci` ports) and to the cloud
fabrics (its `uplink` ports) are the colocation operator's cross-connect, not fibre we pull, so the model is a
`TopologyPhysicalCircuit` with the virtual circuits that ride it (`TopologyInterconnectCircuit` for a DCI,
`TopologyHandoffCircuit` for a cloud or provider handoff) — see `data/demos/30_all/08_interconnects/`. They also need
both endpoints, and a colocation-side generator can only see one. In this demo, which loads no data centers and no
clouds, those ports stay `free` by design. The same applies between two zones of one metro: FR6 reaches the on-ramp in FR2 over a cross-connect (a circuit between zones), never
over a cable. The reverse also holds: a hop inside one cage, such as the patch from an edge to the colocation operator's
own port in the same building (the Equinix Fabric port of the FR2 IBX), is a cable, never a circuit.

The edge pair, its interlink, its FABRIC iBGP session and the edge<->firewall cabling all stay inside **one** zone.
Spread a pair across two zones and none of those would be generated, because the pair is the unit of one
`fabric_templates` entry on one zone.

**Links involving virtual kit.** A virtual colocation gets no cabling: two rented instances are joined by a
connection inside the provider's platform, which is neither a cable nor something this schema has a node kind for.
`AM-NE` therefore has two routers (`eg-am-ne01`, `eg-am-ne02`) and no cables. They are two independent appliances: a
router pair has no redundancy representation in the model, and each is its own Terraform device. A firewall pair would
be a `ManagedFirewallHA` (`active-passive`, an Equinix cluster) whose members the provider wires, so it gets no sync
port or cable.

### Physical vs virtual

There is no `deployment_type` any more: the node kind says what the kit is.

| Node kind | Means | Templates allowed |
| ----------------------------- | ------------------------------------------- | ------------------------------- |
| `TopologyColocationZone` | our own hardware in cage space we rent | `TemplateDcimPhysicalDevice` |
| `TopologyVirtualColocation` | a provider-hosted instance we rent | `TemplateCloudNetworkAppliance` |

Frankfurt and Paris zones name an N9K. Equinix Amsterdam is one virtual colocation, `AM-NE` (the metro is `AM`; metro,
zone and virtual colocation names share the device-name and pool-name space, so they must differ), and names
`C8000V_EDGE`, so it gets `CloudNetworkAppliance`s — an Equinix Network Edge instance, not hardware, with interfaces,
BGP and a config like any device, but consumed like a cloud resource. It has no hardware, so the template's ports are
virtual NICs (`TemplateDcimVirtualInterface`, no cable, no host) and a physical template there would be rejected.

`AM-NE` is routed to Terraform (`infrastructure_orchestrator: terraform`) and ordered under one `CloudAccount`,
`equinix-network-edge` (`equinix/am/02_account.yml`): its `account_id` is the Equinix billing account number (a demo
value), its provider is Equinix (P016) and its owner Internal (P028), whose `notification_emails` are the people the
provider notifies. The ordering parameters of each appliance come from structured models, through its template: `type_code`
is the Device Type name (`C8000V`) and `core_count` its `cpu_count`; `package_code` and `version` are the Software
Image's `package` and `version` (`cisco_xe-17.12.01a`: `network-essentials`, `17.12.01a`); `term_length` is the
appliance's. The generated `virtual-colocation-terraform` artifact prints all of
it; licence tokens are Terraform variables and never live in Infrahub.

### Zones with no kit

Megaport's MCR zones and all three CoreSite metros' zones declare no `fabric_templates`. That is deliberate, not an
omission: an MCR is Megaport's own virtual router in Megaport's cloud (we buy a port on it, we do not deploy into
it), and the CoreSite metros are cage space we rent without running an on-ramp. Every metro, zone and virtual
colocation still joins its generator group (`colocation_metros`, `colocation_zones`, `virtual_colocations`), because
the `created` trigger fires per kind and Infrahub fails a generator run whose target sits outside the definition's
group — so the generator runs and no-ops.

---

## Quick Start (For the Financially Brave)

```bash
# Create a branch — never load demo data straight into main
uv run infrahubctl branch create pop_deployment

# Load the colocation structure (bootstrap must already be loaded)
uv run infrahubctl object load data/demos/10_colocation/ --branch pop_deployment
```

The generators fire automatically on each metro, zone and virtual colocation as it is created — no manual run needed. When the tasks finish the
branch holds eight devices that are in none of these files: `eg-fr201`, `eg-fr202`, `fw-fr201`, `fw-fr202`, `eg-pa401`,
`eg-pa402`, `eg-am-ne01`, `eg-am-ne02` — plus Frankfurt's five cables, with every cabled port flipped from `DcimInterface`'s
`free` default to `active`.

To re-run one zone by hand (idempotent — same devices, same addresses, same cables):

```bash
uv run infrahubctl generator add_colocation_zone name=FR2 --branch pop_deployment
```

A virtual colocation is re-run the same way with `add_virtual_colocation name=AM-NE`.

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
- The virtual colocation `AM-NE` holds no racks, which makes the capacity planning meetings refreshingly short
