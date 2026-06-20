# Datacenter — The Hardware That Started It All

> *"I asked ChatGPT to design our datacenter network. It produced a beautiful
> three-tier architecture diagram with perfect symmetry and zero port utilization data.
> We hired it as a consultant. It now attends all architecture meetings and never
> disagrees with anyone. Morale has never been lower."*

---

## Overview

**Sites:** DC1, DC2, DC3 | **Racks:** Many | **Cables:** More | **Mysteries:** Several

Three datacenters, each with their own personality disorder, sharing a common IP namespace
and a mutual agreement not to ask too many questions about DC2.

---

## The Lineup

| DC | Vibe | Platform | Special Feature |
| --- | --- | --- | --- |
| DC1 | Professional. Works. | Arista EOS + Cisco NX-OS | The one you demo to customers |
| DC2 | Fine. Probably fine. | Dell SONiC + Nokia SR-OS | The one that's "being refactored" |
| DC3 | Friday afternoon energy | Mixed | Three hypervisors. No regrets. |

---

## DC1 — `dc1/`

The flagship. Two pods, full cabling, firewalls, server segments, MLAG, LAGs,
management capabilities, and a `TOPOLOGY.md` because someone cared enough to draw a diagram.

DC1 is the datacenter equivalent of a well-ironed shirt — everything is where it should be,
the cables are labelled, and the documentation is not actively lying.

```text
dc1/
├── 01_locations.yml      # Rack positions. Physically accurate. Spiritually aspirational.
├── 02_topology.yml       # Super-spines, pods, spines, leaves. The full family.
├── 03_racks.yml          # Named. Numbered. Beloved.
├── 04_devices.yml        # Every device gets a name. Every name tells a story.
├── 05_cabling.yml        # 400+ cables. All correct. We checked. Twice.
├── 05_capabilities.yml   # NTP, Syslog, AAA. Because bare metal needs feelings too.
├── 06_lag_mlag.yml       # Port-channels. Because redundancy is love.
├── 07_security.yml       # Firewalls. Physical AND virtual. Belt AND suspenders.
├── 08_server_segments.yml # Servers. VLANs. The reason any of this exists.
└── TOPOLOGY.md           # A diagram. Hand-crafted with Mermaid. With love.
```

---

## DC2 — `dc2/`

DC2 exists in a state of dignified ambiguity. It has all the same files as DC1.
It runs Dell SONiC on the fabric and Nokia SR-OS on the edges.
Nobody is entirely sure when it was last touched, but it loads cleanly and that's enough.

> *DC2 is the infrastructure equivalent of a fridge that's been there since you moved in.
> You don't question it. It works. You leave it alone.*

---

## DC3 — `dc3/`

DC3 is a special case. DC3 was built on a Friday, with three different hypervisor platforms,
because three separate teams each won a budget approval in the same quarter
and nobody wanted to have the unification conversation.

```text
dc3/
├── 01_locations.yml           # It's in a datacenter. That's all we know.
├── 02_topology.yml            # 4 pods. Index starts at 1, not 3. This was a journey.
├── 03_racks.yml               # Racks across 4 pods. Each rack knows which pod it's in now.
├── 04_devices.yml             # K8s workers, vSphere ESXi hosts, Nutanix nodes. All in one building.
├── 05_cabling.yml             # 1500+ lines. It's fine. Everything's fine.
├── 06_lag_mlag.yml            # LAGs so the cables have friends.
├── 08_server_segments.yml     # Segments for all three platforms. Fair and equal.
├── 09_virt_cluster.yml        # Three clusters. One namespace. Zero consensus.
└── 10_cluster_capabilities.yml # 18 capability nodes. Completely reasonable.
```

### DC3 Hypervisor Situation (A Brief History)

- **2019 Q1:** Team A evaluates Kubernetes. Approves Kubernetes. Kubernetes is deployed.
- **2019 Q2:** Team B evaluates vSphere. Approves vSphere. vSphere is deployed.
- **2019 Q3:** Team C evaluates Nutanix. Approves Nutanix. Nutanix is deployed.
- **2019 Q4:** Consolidation meeting scheduled. Rescheduled. Cancelled. Forgotten.
- **2024:** All three platforms are modeled in Infrahub with full capability nodes.
  Someone called this "unified visibility." It is the kindest possible framing.

An AI was asked whether DC3 should consolidate to a single hypervisor.
It said "it depends." It was right. We're still deciding.

---

## Segments — `segments/`

VLAN-backed network segments with ambitions above their station. They used to be called VLANs.
They are now called Segments. The underlying technology is identical.
The self-esteem improvement was measurable.

```text
segments/
└── 07_segments.yml  # All the segments. Customer tenants, production, staging, AKS, all of it.
                     # Named carefully so future engineers can tell them apart.
                     # They named some of them "customer-1-aks-zone1-production" and we respect the commitment.
```

---

*No hypervisors were decommissioned in the writing of this README.
They remain. They will always remain. This is the way.*
