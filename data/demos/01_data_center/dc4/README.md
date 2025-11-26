# DC4 - Mixed Deployment Data Center (The Berlin Techno Edition)

## Overview
**Location:** Berlin 🇩🇪 (The hipster capital - your infrastructure is as edgy as the local techno scene)

**Size:** Small (S) - Flexible, creative, and a little chaotic

**Platform:** Edgecore with SONiC - Vendor-neutral open source networking

**Design Pattern:** S-Mixed (Small Mixed Deployment)

**Use Case:** When the architecture team can't agree on middle_rack vs flat ToR and someone says "why not both?" Pod 1 goes full mixed deployment, Pod 2 goes pure flat ToR. It's like having a hybrid car that's also a motorcycle. Confusing? Yes. Flexible? Absolutely.

---

## Architecture (Identity Crisis with a Beat)

### Fabric Scale
- **Super Spines:** 2 (Edgecore 7726-32X-O)
- **Total Pods:** 2
- **Total Spines:** 5 (3 in Pod 1, 2 in Pod 2)
- **Total Racks:** 5
- **Deployment Types:** mixed (Pod 1), tor (Pod 2)

### Pod Structure (Split Personality Disorder)
| Pod   | Spines | Racks | Deployment | Personality                |
|-------|--------|-------|------------|----------------------------|
| Pod 1 | 3      | 2     | mixed      | The Sophisticated Engineer |
| Pod 2 | 2      | 3     | tor        | The Pragmatic Minimalist   |

---

## Hardware Stack (Edgecore All the Things)

### Super Spine Layer
- **Model:** Edgecore 7726-32X-O
- **Ports:** 32x100GbE
- **Role:** Inter-pod negotiators
- **SONiC OS:** Open networking for the brave

### Spine Layer
- **Pod 1:** Edgecore 7726-32X-O × 3 spines
- **Pod 2:** Edgecore 7726-32X-O × 2 spines
- **Ports:** 32x100GbE each
- **Role:** Aggregating both leafs AND direct ToR connections (mixed life)

### Leaf Layer (Pod 1 Only)
- **Model:** Edgecore 7726-32X-O
- **Role:** Rack-level aggregation

### ToR Layer
- **Model:** Edgecore 7726-32X-O
- **Role:** Server connectivity

---

## Deployment Strategy (Mixed Mastery)

**Mixed Deployment Mystery:**
- ToRs in racks WITH leafs connect locally
- ToRs in racks WITHOUT leafs find the least-utilized leaf in the pod
- It's like musical chairs, but for network cables

**Why Mixed Deployment Rocks:**
- ✅ Flexibility for different rack types
- ✅ Efficient use of leafs
- ✅ Adaptable to changing requirements

**Trade-offs:**
- ⚠️ More complex cabling
- ⚠️ NOC team needs extra coffee
- ⚠️ Documentation is critical

---

## Quick Start

```bash
uv run infrahubctl branch create dc4-test-$(date +%s)
uv run invoke deploy-dc --scenario dc4 --branch dc4-test-<timestamp>
```

**What Happens:**
- Creates super spines, pods, spines, racks, leafs, ToRs, cables, configs
- Validates connectivity

---

## Troubleshooting (When Berlin Goes Full Techno)
- "Pod disappeared!" → Re-run full deploy, generators now have protection
- "More racks than expected" → Check branch, clean up extra racks
- "Configs not generating" → Check templates, branch, logs
- "Validation failing" → Review messages, check cabling, verify configs

---

## Real Talk (Production Conversation)
- **Production-Ready:** Hardware, patterns, architecture
- **Demo-Land:** Templates, minimal security, simplified configs
- **To Go Production:** Harden configs, add policies, integrate monitoring, test failures, document

**Bottom Line:** Use DC4 to learn, not to deploy on Friday afternoon.

---

## Related Scenarios
- **DC1:** The Kitchen Sink (3 pods, 28 racks)
- **DC2:** The Parisian Café (Middle Rack)
- **DC3:** The Purist (Flat ToR)
- **DC5:** The United Nations (Multi-vendor)

---

## Fun Fact
Mixed deployment is the "I want my cake and eat it too" of network design. The cost is explaining this to the night shift NOC team.
