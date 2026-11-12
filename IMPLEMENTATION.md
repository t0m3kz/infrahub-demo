# Implementation Guide: Data Center Fabrics

*The part of the design that usually lives in one architect's head and leaves with them.*

This guide explains how the data center generators turn a fabric design into devices, cables and names. It is
written for network architects and operators who author topology data (and for whoever inherits the fabric after
them). For the full data model rules see
[AGENTS.md](AGENTS.md); for the development workflow see [CONTRIBUTING.md](CONTRIBUTING.md).

## Table of Contents

1. [Fabric Designs](#fabric-designs)
2. [Pod Deployment Types](#pod-deployment-types)
3. [Spine Port Allocation](#spine-port-allocation)
4. [Routing Strategies](#routing-strategies)
5. [Device Naming Conventions](#device-naming-conventions)
6. [Cable Attributes](#cable-attributes)
7. [Capacity Validation](#capacity-validation)

---

## Fabric Designs

Two required design templates bound every fabric. They hold only the envelope (the `max_*` values); the records
live in [data/bootstrap/14b_fabric_templates.yml](data/bootstrap/14b_fabric_templates.yml). Every pool size, port
stride and loopback or technical prefix is derived from them by
[generators/fabric_sizing.py](generators/fabric_sizing.py) before any rack exists.

### Data center designs (`TopologyDataCenter.design`, `TopologyTemplateDC`)

| Design | Max pods | Super-spines | Hyper-spines | DC border-leafs | Shape |
| ------ | -------- | ------------ | ------------ | --------------- | ----- |
| `S` | 2 | 0 | 0 | 0 | Micro-fabric: pods back-to-back, no DC-level border-leaf tier |
| `M` | 4 | 0 | 0 | 2 | Pods back-to-back, with a DC-level border-leaf tier |
| `L` | 8 | 4 | 0 | 2 | Classic 3-tier Clos: leaf, spine, super-spine |
| `XL` | 16 | 4 | 2 | 4 | 3-tier Clos plus a hyper-spine tier |

Every DC design lists all six pod designs in `pod_templates`; a pod's design must be one of its DC's.

### Pod designs (`TopologyPod.design`, `TopologyTemplatePod`)

Pod designs are named `{SIZE}_{DEPLOYMENT}`:

| Design | Rows | Compute racks / row | Network racks / row | Leafs / network rack | ToRs / compute rack | Spines | Border-leafs |
| ------ | ---- | ------------------- | ------------------- | -------------------- | ------------------- | ------ | ------------ |
| `S_MIDDLE` | 2 | 8 | 1 | 4 | 0 | 2 | 1 |
| `S_TOR` | 2 | 8 | 0 | 0 | 2 | 4 | 1 |
| `S_MIXED` | 2 | 8 | 1 | 2 | 2 | 4 | 1 |
| `S_BORDER_SPINE_POD` | 1 | 0 | 1 | 8 | 0 | 2 | 0 |
| `M_MIXED` | 4 | 8 | 1 | 2 | 2 | 3 | 1 |
| `M_MIDDLE` | 4 | 8 | 1 | 4 | 0 | 3 | 1 |

The number of switches per tier follows from the envelope:

- ToRs = `max_rows x max_compute_racks_per_row x max_tors_per_compute_rack`
- Leafs = `max_rows x max_network_racks_per_row x max_leafs_per_network_rack`

---

## Pod Deployment Types

Pick one before the procurement committee picks the switches for you. The deployment type of a pod is **derived from
its design, never stored**
([generators/pod_config.py](generators/pod_config.py) `pod_deployment_type`):

| Design has | Deployment type |
| ---------- | --------------- |
| No ToRs | `middle_rack` |
| ToRs and no leafs | `tor` |
| ToRs and leafs | `mixed` |

The roles a rack may hold depend on the type ([generators/rack.py](generators/rack.py)
`ROLES_BY_DEPLOYMENT_TYPE`):

| Deployment type | Roles a rack may hold |
| --------------- | --------------------- |
| `middle_rack` | `leaf`, `l2_leaf`, `access_leaf` |
| `tor` | `tor` |
| `mixed` | `leaf`, `tor`, `l2_leaf`, `access_leaf` |

`tor` and `l2_leaf`/`access_leaf` are mutually exclusive in one rack: a ToR cables to the spines, an L2 or access
leaf cables to a local leaf pair, and no cabling plan does both. Border-leafs, firewalls and load balancers are not
rack-level roles: they are declared on the data center's own `fabric_templates` and placed by the DC generator.

### Middle rack

The network rack of each row holds the leafs, which cable to the pod spines. Compute racks have no ToRs; L2 and
access leafs cable to the leaf pair of their own rack or of the network rack in the same row.

```text
Pod
├── spine-01, spine-02           (all leafs cable here)
├── Row 1
│   ├── Network rack: leaf-01..04 ──── spines
│   │                 l2-leaf pair ─── local leafs
│   └── Compute racks: servers ─────── row leafs / l2-leafs
└── Row 2
    └── (same pattern)
```

Use it for dense rows where each row has its own aggregation, with short intra-row cabling (easier troubleshooting,
the 2am on-call engineer's favourite property).

### ToR

No leafs anywhere in the pod. Every compute rack holds ToRs that cable straight to the spines: a two-tier
spine-ToR design.

```text
Pod
├── spine-01..04                 (all ToRs cable here)
├── Row 1
│   ├── Compute rack 1: tor-01, tor-02 ──── spines
│   └── Compute rack 2: tor-01, tor-02 ──── spines
└── Row 2
    └── (same pattern)
```

Use it for small pods with enough spine port density and no need for an aggregation layer. No leaf switches means a
lower bill, and finance reads that line first.

### Mixed

Network racks hold leafs and compute racks hold ToRs; **both cable to the spines**. A compute rack may hold L2 or
access leafs instead of ToRs; those cable to the leaf pair in the network rack of their row.

```text
Pod
├── spine-01..04                 (leafs and ToRs cable here)
├── Row 1
│   ├── Network rack: leaf-01, leaf-02 ──── spines
│   ├── Compute rack A: tor-01, tor-02 ──── spines
│   └── Compute rack B: l2-leaf pair ────── row 1 leafs
└── Row 2
    └── (same pattern)
```

Use it for large pods that combine directly attached compute racks with row-level aggregation. The committee
compromise, in rack form.

### Comparison

| Feature | Middle rack | ToR | Mixed |
| ------- | ----------- | --- | ----- |
| Tiers below the spine | Leaf (+ L2/access leaf) | ToR | Leaf and ToR side by side |
| Spine downlinks used by | Leafs, border-leafs | ToRs, border-leafs | Leafs, border-leafs, ToRs |
| Compute rack attaches to | Row leafs | Spines (via its ToRs) | Spines (ToRs) or row leafs (L2/access leafs) |
| Typical design | `S_MIDDLE`, `M_MIDDLE` | `S_TOR` | `S_MIXED`, `M_MIXED` |

---

## Spine Port Allocation

Every leaf, ToR and border-leaf takes one downlink port on every spine of its pod. The ports are laid out in fixed
blocks computed from the **design envelope, not from what is built**, so the blocks never overlap however a pod
fills up ([generators/pod_config.py](generators/pod_config.py) `spine_downlink_layout`):

| Block | Middle rack / Mixed | ToR |
| ----- | ------------------- | --- |
| Leafs | Start at port 0; the network rack of row `r` starts at `(r - 1) x leafs per network rack` | (none) |
| Border-leafs | Right after the leafs | Right after the ToRs |
| ToRs | After the leafs and border-leafs (mixed only) | Start at port 0 |

A ToR slot is reserved for every rack position of every row (compute plus network racks: a mixed row numbers
both), so a spine of a given design needs:

```text
ports = leafs + max_border_leafs_per_pod + max_rows x (compute + network racks per row) x ToRs per rack
```

---

## Routing Strategies

`TopologyDataCenter.routing_strategy` selects the fabric protocols:

| Strategy | Underlay | Overlay |
| -------- | -------- | ------- |
| `ebgp-ebgp` | eBGP, one ASN per device from the DC's fabric ASN pool (RFC 7938) | eBGP EVPN |
| `ebgp-ibgp` | eBGP, one ASN per device | iBGP EVPN |
| `ospf-ibgp` | OSPF | iBGP EVPN on a shared AS |

Spines own an `underlay` BGP process for the eBGP strategies and only the shared-AS `overlay` process for
`ospf-ibgp`; a rack waits for the matching spine process before it is built.

---

## Device Naming Conventions

`naming_convention` on the deployment selects one of four strategies (default `standard`,
[generators/helpers/naming.py](generators/helpers/naming.py)). All four use short role codes and keep the full
location path, including the DC's fabric index (one DC name may host several fabrics). Renaming every device after
go-live is possible, in the way that moving a data center over a long weekend is possible.

| Role | Code | Role | Code |
| ---- | ---- | ---- | ---- |
| `spine` | `sp` | `border-leaf` | `bl` |
| `super-spine` | `ss` | `tor` | `tor` |
| `hyper-spine` | `hs` | `l2-leaf` | `l2` |
| `border-spine` | `bs` | `access-leaf` | `al` |
| `edge` | `eg` | `access-switch` | `as` |
| `leaf` | `lf` | `distribution-switch` | `ds` |
| `internal-firewall` | `fw` | `external-firewall` | `xf` |
| `load-balancer` | `lb` | | |

A role without a code keeps its full name. The location path is ordered fabric, pod, suite, row, rack.

| Strategy | Format | Example |
| -------- | ------ | ------- |
| `standard` | Role code, separator, fabric name, location indexes concatenated (unpadded), role index (padded) | `lf-dc11312401` (fab 1, pod 3, suite 1, row 2, rack 4, leaf 1); DC-scoped `ss-dc1101` |
| `hierarchical` | Fabric name, each location index, then role code and index, joined with `.` | `dc1.2.3.lf01` |
| `flat` | The hierarchical components with no separator | `dc123lf01` |
| `computed` | Fabric name, separator, role code, then a fixed-width run of two digits per level | `dc1-lf020301` (fab 2, pod 3, leaf 1) |

Notes:

- `standard` is the most readable in a device list and the default, though not a hostname you type from memory at
  2am; your future self, grepping logs in the middle of an outage, will be grateful. Two adjacent double-digit indexes
  can be ambiguous to parse back; use `computed` when names must be parsed unambiguously.
- `hierarchical` suits tooling that reads positions rather than labels.
- `flat` is the shortest form, for systems with strict hostname-length limits (and for the lab that quietly became
  production).

---

## Cable Attributes

Every generator-created `DcimCable` carries derived attributes
([generators/helpers/cable_attributes.py](generators/helpers/cable_attributes.py)); a rerun writes the same values.
Cables declared in data are not generated and keep their attributes unset. The point is accurate inventory instead of
a guess at the patch panel about what the spreadsheet meant.

| Attribute | Rule |
| --------- | ---- |
| `type` | `copper` when both ends are copper interfaces, otherwise `mmf` ([generators/helpers/cabling.py](generators/helpers/cabling.py)) |
| `cable_id` | `CBL-` plus the first 10 upper-case hex digits of the SHA-1 of the two sorted endpoint labels (`<device>-<interface>`); depends only on the ends, so it is stable across reruns |
| `color` | The TIA-598 jacket colour of the type (for example `smf` yellow, `mmf` aqua) |
| `length_cm` | Derived from the suite layout, rounded up to the next 50 cm |

Both ends copper is also how you find the switch that has been "temporary" since 2019.

The length uses the suite grid (`rows`, `racks_per_row`) and its pitches (`rack_pitch_cm` along a row,
`row_pitch_cm` between rows), with 50 cm of service loop per end:

- Same rack: 100 cm, no slack added.
- Same row: `|index_a - index_b| x rack_pitch + 100 cm`.
- Different rows: `|row_a - row_b| x row_pitch` plus the walk to the aisle at the cheaper end of the row (both
  racks cross at the same end) plus 100 cm.

The length stays unset when it would be a guess: an end not in a rack, racks in different suites, a missing pitch,
or a rack coordinate outside the suite grid.

---

## Capacity Validation

Racks and suites are data; nothing generates a `LocationRack`. The checks hold them to the designs, rather than
letting you discover on rack-and-stack day that the design never fitted the room:

- [checks/suite_capacity.py](checks/suite_capacity.py): every rack sits inside its suite's grid
  (`row_index <= rows`, `index <= racks_per_row`), and each suite that hosts a rack of a pod fits the pod's design
  envelope (`max_rows <= rows`, `max_compute_racks_per_row + max_network_racks_per_row <= racks_per_row`). The
  envelope, not the racks present today, is the demand, so a pod can always grow into its design.
- The rack generator refuses a rack whose roles do not match the pod's deployment type, or that exceeds the
  design's per-rack device limits.

Skipped rather than reported: a rack without a suite or without numeric coordinates, a rack hosted by something
without a design (a colocation zone), and a pod without racks.
