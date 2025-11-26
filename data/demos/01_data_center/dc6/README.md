# DC6 - Mixed Vendors Within Pods Data Center (The Polish Efficiency Edition)

## Overview
**Location:** Katowice 🇵🇱 (Poland's industrial powerhouse turned tech hub—where the only thing faster than the fiber is the coffee. Half the cost of Western Europe, double the sarcasm, and the rolada-modro-kapusta-gumiklyjzy-to-latency ratio is unbeatable!)

**Size:** Medium (M) - Cost-effective, interoperable, and a little wild. Big enough to cause trouble, small enough to blame someone else.

**Platform:** Multi-Vendor (Cisco, Arista, Dell SONiC, Edgecore SONiC) — because why settle for one vendor's bugs when you can have them all?

**Design Pattern:** M-Multi-Layer-Mixed (Medium Multi-Vendor Mixed Layers) — the architectural equivalent of a buffet: a little bit of everything, and you never know what you'll get next.

**Use Case:**
Medium-sized multi-vendor data center with middle_rack deployment. It's the perfect playground for engineers who like living dangerously, managers who love vendor bingo, and auditors who enjoy existential dread. Demonstrates vendor interoperability at a scale just big enough to break things, but small enough to blame the intern. Cost-effective multi-vendor approach for medium enterprises—because why settle for one vendor's support hotline when you can have four? If you've ever wanted to see a Cisco, Arista, Dell, and Edgecore device argue about spanning tree, BGP, and whose logo is the ugliest, this is your chance. Warning: May cause spontaneous VLAN migrations, philosophical debates about port-channel naming, and a sudden urge to update your resume.

---

## Architecture (Layer-Level Vendor Mix)

### Fabric Scale
- **Super Spines:** 2 (Cisco N9K-C9336C-FX2)
- **Total Pods:** 2
- **Total Spines:** 4 (2+2 across pods)
- **Total Racks:** 4 (2 per pod)
- **Deployment Type:** middle_rack (all pods)

### Pod Structure (Vendor Mix Table)
| Pod   | Spines | Vendor                | Leafs/ToRs Vendor | Racks | Deployment   |
|-------|--------|----------------------|-------------------|-------|--------------|
| Pod 1 | 2      | Arista (DCS-7050CX3-32C-R) | Dell SONiC      | 2     | middle_rack  |
| Pod 2 | 2      | Edgecore (7726-32X-O)      | Cisco NX-OS     | 2     | middle_rack  |

---

## Hardware Stack (Multi-Vendor Mayhem)

### Super Spine Layer
- **Model:** Cisco N9K-C9336C-FX2
- **Ports:** 36x100GbE
- **Role:** Inter-pod connectivity
- **Fun Fact:** The neutral overlords

### Spine Layer (Multi-Vendor)
- **Pod 1:** Arista DCS-7050CX3-32C-R (2 spines)
- **Pod 2:** Edgecore 7726-32X-O (2 spines)
- **Ports:** 32x100GbE each
- **Role:** Pod-level aggregation

### Leaf/ToR Layer
- **Pod 1:** Dell SONiC
- **Pod 2:** Cisco NX-OS
- **Role:** Rack-level aggregation and server connectivity

---

## Quick Start

```bash
uv run infrahubctl branch create dc6-test-$(date +%s)
uv run invoke deploy-dc --scenario dc6 --branch dc6-test-<timestamp>
```

**What Happens:**
- Creates super spines, pods, spines, racks, leafs, ToRs, cables, configs
- Validates connectivity

---

## Troubleshooting (When Pierogi Meet Packets)
- "Pod disappeared!" → Re-run full deploy, generators now have protection
- "More racks than expected" → Check branch, clean up extra racks
- "Configs not generating" → Check templates, branch, logs
- "Validation failing" → Review messages, check cabling, verify configs

---

## Real Talk (Production Conversation)
- **Production-Ready:** Hardware, patterns, architecture
- **Demo-Land:** Templates, minimal security, simplified configs
- **To Go Production:** Harden configs, add policies, integrate monitoring, test failures, document

**Bottom Line:** Use DC6 to learn, not to deploy on Friday afternoon.

---

## Related Scenarios
- **DC1:** The Kitchen Sink (3 pods, 28 racks)
- **DC2:** The Parisian Café (Middle Rack)
- **DC3:** The Purist (Flat ToR)
- **DC4:** The Variety Pack (Mixed deployments)
- **DC5:** The United Nations (Multi-vendor)

---

## Fun Fact
Polish efficiency meets vendor chaos—half Western European cost, full interoperability headaches. The pierogi are always fresh, the troubleshooting never ends.
