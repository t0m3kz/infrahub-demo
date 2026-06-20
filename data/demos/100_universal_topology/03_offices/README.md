# Offices — Where the Network Meets the People Who Break It

> *"We gave users a 1Gbps uplink. They used it to stream 4K video during the all-hands call.
> We asked an AI to solve the QoS problem. It suggested educating the users.
> The AI has clearly never met our users."*

---

## Overview

**Sites:** London, Edinburgh, Barcelona, Madrid, Stuttgart | **Connectivity:** SD-WAN + MPLS + Internet
**Primary user activity:** Video calls, shadow IT, and asking IT why the Wi-Fi is slow

Five offices across Europe, each with their own network personality and a shared talent
for generating support tickets at the worst possible moment.

---

## The Offices

| Site | City | Role | Personality |
| --- | --- | --- | --- |
| `lon-branch` | London | Branch | Has opinions. All of them. |
| `edi-branch` | Edinburgh | Branch | Reliable. Quietly judgmental. |
| `bcn-branch` | Barcelona | Branch | Summer hours apply to the router. |
| `mad-regional` | Madrid | Regional Hub | Important. Untouched since deployment. |
| `str-branch` | Stuttgart | Branch | Will ask about PROFINET. Every time. |

---

## London — `lon-branch/`

The UK's contribution to the topology. Connected via BT internet because the
procurement team got a good deal in 2022 and "good enough" became "permanent."

Features SD-WAN uplinks, patch panels, and the quiet dignity of an office that
survived Brexit, two CTOs, and a floor move without losing its BGP sessions.

> *"London branch goes down? No one notices for 45 minutes because everyone
> assumed it was a Teams issue."*

---

## Edinburgh — `edi-branch/`

Edinburgh: where the infrastructure is as solid as the castle and considerably
less dramatic. Connected via BT, running AM1 SD-WAN, doing exactly what it's told.

The Edinburgh branch has never filed a P1 incident. Nobody talks about this openly,
but everyone knows. It is a point of quiet pride in the NOC.

---

## Barcelona — `bcn-branch/`

Barcelona runs perfectly well between 9am and 2pm and again between 4pm and 7pm.
The 2-hour gap is non-negotiable and not reflected in the SLA.

The network equipment respects local customs. The monitoring alerts do not.
This is an ongoing conversation.

---

## Madrid — `mad-regional/`

Madrid is the regional hub — the important one, the big one, the one with redundant
uplinks and a proper rack instead of a patch panel on a shelf.

It was last modified during initial deployment. This is either a sign of perfect
stability or a sign that nobody dares touch it. The answer is both.

> *An AI analyzed the Madrid config and said it was "production-ready and well-structured."
> We don't know if it was complimenting us or just trained on our own documentation.*

---

## Stuttgart — `str-branch/`

Stuttgart: home of automotive precision, engineering excellence, and the one network
engineer who will, without fail, ask whether the switching fabric supports PROFINET.

It does not. It supports the things that enterprise networks support.
The conversation happens anyway. Every quarter.

The Stuttgart branch connects via MPLS and SD-WAN and has never once complained
about latency. The users have. The network has not.

---

## Connectivity Model

All offices connect back to Equinix via SD-WAN or MPLS, because a hub-and-spoke
model with centralized internet breakout was the right call in 2019 and
will be "reviewed next quarter" indefinitely.

```text
Office → SD-WAN/MPLS → Equinix → DC1/DC2 → Happiness
                               ↘ Cloud → Acceptable Latency
```

---

## A Note on Office Networking

An AI was once asked to predict which office would file the most tickets.
It refused to answer, citing "insufficient data" and "potential bias."
The answer is Barcelona. It was always Barcelona. Everyone knows this.

---

*All offices are modeled with appropriate redundancy and professional dignity.
The users are not modeled. Some things are beyond the scope of network automation.*
