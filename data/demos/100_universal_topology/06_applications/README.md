# Applications — The Reason the Infrastructure Exists

> *"We built a beautifully segmented, fully redundant, cross-continental network.
> Then someone asked what it was for.
> The answer, it turns out, was: a trade portal, a shipment tracker, and a compliance system.
> The fiber cost more than the applications. This is normal."*

---

## Overview

The application layer maps **business applications** to the underlying infrastructure —
VMs, clusters, segments, load balancers, and network namespaces — inside the same
unified graph as the physical topology.

This means you can ask: *"which datacenters does the trade-portal production environment
touch, and which network segments are in its path?"* — and get an answer in a single query,
not a ticket to four teams.

---

## Customer Portfolios

| Customer | Org ID | Applications | Environment |
| --- | --- | --- | --- |
| Nordix Ltd. | C001 | trade-portal, auth-platform, risk-engine, nlb-app | production + non-prod |
| SwiftGo GmbH | C002 | shipment-tracker, fleet-manager | production + non-prod |
| Vaultex Inc. | C003 | vault-core, compliance-portal | production |

---

## C001 — Nordix Ltd

The flagship customer. Three production applications, each decomposed into frontend,
backend, and database components, mapped to VM capabilities and VXLAN segments.

```text
C001/
├── 00_namespaces.yml     # IP namespaces: C001-PROD, C001-NONPROD
├── 01_segments.yml       # Network segments per tier: web-frontend, app-backend, database, cache
├── 02_portfolio.yml      # AppPortfolio: owner C001
├── 03_applications.yml   # trade-portal (p+n), auth-platform (p), risk-engine (p), nlb-app (p)
├── 04_vms.yml            # DcimVirtualDevice nodes per segment
├── 06_components.yml     # AppComponent: segment + VM capability mapping
└── 07_nlb_listeners.yml  # NLB listener config for nlb-app
```

**Application → segment → VM mapping (production):**

| Application | Component | Network Segment | VMs |
| --- | --- | --- | --- |
| trade-portal | frontend | c001-trade-portal-frontend-p | C001-WEB-VM-01, -02 |
| trade-portal | backend | c001-trade-portal-backend-p | C001-APP-VM-01..04 |
| auth-platform | frontend | c001-auth-frontend-p | C001-WEB-VM-03 |
| auth-platform | backend | c001-auth-backend-p | C001-APP-VM-05..06 |
| risk-engine | backend | c001-risk-backend-p | C001-APP-VM-07..08 |
| all | database | c001-database-p | C001-DB-VM-01..04 |

---

## C002 — SwiftGo GmbH

Logistics. Two applications: one that tracks your shipments, one that manages the fleet
doing the tracking. Both exist in production and non-production environments.
The non-prod environment has fewer VMs. We chose not to ask whether it has fewer bugs.

```text
C002/
├── 00_namespaces.yml
├── 01_segments.yml       # web-frontend, app-backend, database per environment
├── 02_portfolio.yml
├── 03_applications.yml   # shipment-tracker (p+n), fleet-manager (p)
├── 04_vms.yml
└── 06_components.yml     # AppComponent mapping for all three applications
```

---

## C003 — Vaultex Inc

Financial services. Two production applications with zero non-production environments
because Vaultex does not believe in staging. They believe in prayer and change windows.

```text
C003/
├── 00_namespaces.yml
├── 01_segments.yml       # vault-core and compliance-portal segments
├── 02_portfolio.yml
├── 03_applications.yml   # vault-core (p), compliance-portal (p)
├── 04_vms.yml
└── 06_components.yml
```

> *Vaultex has never had a staging environment. Their MTTR is surprisingly low.
> Their change management process is described internally as "very thorough."
> Nobody has asked to see it.*

---

## What You Can Query

Once loaded, the application layer enables queries like:

**Which VMs serve trade-portal production?**

```text
AppApplication(c001-trade-portal-p) → AppComponent → capabilities → DcimVirtualDevice
```

**Which network segments does risk-engine touch?**

```text
AppApplication(c001-risk-engine-p) → AppComponent → network_segment → ManagedNetworkSegment
```

**Which VXLAN segments route through a firewall?**

```text
ManagedNetworkSegment(pbr_enabled=true) → firewall → DcimPhysicalDevice
→ AppComponent → AppApplication → AppPortfolio
```

**Full path: application → VM → segment → DC fabric → cloud:**

```text
c001-trade-portal-p → C001-WEB-VM-01 → c001-trade-portal-frontend-p
→ [VXLAN segment] → DC3-K8S-PROD (or CUST1-EKS-EU-CENTRAL-1)
→ [physical cluster] → [DC fabric] → [Equinix] → [Direct Connect]
→ AWS eu-central-1
```

The application layer does not change the traversal. It extends it upward.
The query that found a physical path between two servers now finds a path
from a business application to a cloud endpoint, because they all live
in the same graph.

---

## The VM in Two Segments Problem

Some VMs appear in multiple `AppComponent` entries with different `network_segment` values.
This is intentional. A VM serving both the frontend and backend tier of an application
(multi-NIC, or multi-service) legitimately belongs to multiple segments.

`AppComponent.network_segment` describes which segment *that component's traffic flows through*,
not an exclusive physical assignment. The segment view is a projection,
not a constraint. This is the correct model.

An AI was asked if this was confusing. It said "yes." We agreed. We kept it anyway.

---

*The applications are the reason the infrastructure exists.
The infrastructure is the reason the applications are still up.
Infrahub is the reason you can see both at the same time.*
