# External — Not Your Network, Still Your Problem

> *"I asked an AI to summarize our external connectivity dependencies.
> It produced a beautiful graph with 47 nodes and the label 'single point of failure'
> on 31 of them. We thanked it for its honesty and changed nothing."*

---

## Overview

**Providers:** BT, BT UK, Colt, DE-CIX | **Partners:** ACME Corp, Globex
**Philosophy:** Trust, but verify. Mostly just verify.

The external layer — everything that lives outside your control, answers to a different
NOC, and has a completely different maintenance window than the one they told you about.

---

## What's Here

```text
04_external/
├── 06_peering-bgp.yml     # BGP peering config. The contracts are longer than this file.
├── internet/              # ISPs. Tier 1 carriers. The backbone of civilisation (and outages).
│   ├── bt/                # BT. British. Reliable. Occasionally very, very not.
│   ├── bt-uk/             # Also BT, but specifically UK-flavoured.
│   ├── colt/              # Colt. The European alternative. Also occasionally not reliable.
│   └── decix/             # DE-CIX. Frankfurt's finest internet exchange. Still Frankfurt.
└── partners/              # Business partners. Modeled with hope and static routes.
    ├── acme/              # ACME Corp. No, not that one. Well. Maybe that one.
    └── globex/            # Globex. Springfield's premier industrial conglomerate.
                           # (The lawyers said we had to say these are fictional.)
```

---

## Internet Providers — `internet/`

Four providers. Three of them are in Frankfurt (disguised as "Amsterdam" and "Paris").
The fourth is BT UK, which is in the UK and has the maintenance windows to prove it.

BGP sessions are established, prefixes are filtered, communities are tagged,
and somewhere in a NOC a human is watching a graph that looks exactly like your traffic graph
but has a completely different response time on the phone.

> *The ISP NOC has been notified. They are investigating.
> They will continue investigating until the issue resolves itself,
> at which point they will close the ticket as "no fault found."*

---

## Partners — `partners/`

Two partner networks, modeled with appropriate optimism. ACME and Globex connect
via cross-connects through Equinix, because everyone eventually ends up at Equinix.

Partner connectivity: 80% business requirement, 20% "our CEO knows their CEO."

> *An AI was asked to assess the risk of partner dependencies.
> It flagged both as "high impact, low control." We already knew this.
> We appreciated the validation. We changed nothing.*

---

*External networks are modeled accurately. External network behaviour is not modeled,
because no schema is expressive enough to capture the chaos of a BGP session
that goes down at 3am on a bank holiday.*
