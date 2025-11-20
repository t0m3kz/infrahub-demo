# Scenario: dc5

**Description:**
Large data center - New York


- High capacity and performance
- Designed for mission-critical workloads
- Demonstrates full-scale enterprise architecture

---
## Deployment Template / Design

- **Design Pattern:** L-Standard-MR
- **Deployment Type:** middle_rack
- **Maximum Super Spines:** 2
- **Maximum Spines:** 4
- **Maximum Pods:** 4
- **Maximum Leafs:** 24
- **Maximum Rack Leafs:** 8
- **Maximum Middle Racks:** 8
- **Maximum ToRs:** 48
- **Naming Convention:** standard

---
## Used Hardware Components

### Super Spines
- Cisco N9K-C9336C-FX2 (Template: N9K_C9336C_FX2_SUPER_SPINE)

### Pods & Spines
- Pod 1: Cisco N9K-C9336C-FX2 (Template: N9K_C9336C_FX2_SPINE, 3 spines)
- Pod 2: Arista DCS-7050CX3-32C-R (Template: DCS-7050CX3-32C-R_SPINE, 3 spines)
- Pod 3: Dell PowerSwitch S5232F-ON (Template: PowerSwitch-S5232F-ON_SPINE, 4 spines)
- Pod 4: Edgecore 7726-32X-O (Template: Edgecore-7726_32X_O_SPINE, 2 spines)

### Racks & Leafs
- Rack-1-x: Cisco N9K Leafs (Template: 4_CISCO_LEAFS_N9K-C9336C-FX2)
- Rack-2-x: Arista Leafs (Templates: 2_ARISTA_LEAFS_DCS-7050CX3-32C-R, 4_ARISTA_LEAFS_DCS-7050CX4M-48D8-F)
- Rack-3-x: Dell Leafs (Templates: 2_DELL_LEAFS_PowerSwitch-S5248F-ON, 4_DELL_LEAFS_PowerSwitch-S5248F-ON)
- Rack-4-x: Edgecore Leafs (Template: 4_EDGECORE_LEAFS_Edgecore-7AS7326-56X-O-48V-F)

### Suites
- Suite-1: Racks 1-x (Cisco)
- Suite-2: Racks 2-x (Arista)
- Suite-3: Racks 3-x (Dell)
- Suite-4: Racks 4-x (Edgecore)

---
## Topology Overview

- 4 pods, each with dedicated spine hardware
- Each pod contains 4 racks with matching leaf hardware
- Suites group racks by vendor/hardware type
