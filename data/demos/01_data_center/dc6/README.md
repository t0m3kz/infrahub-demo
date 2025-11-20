# Scenario: dc6

**Description:**
Large data center - New York


- Similar to dc5, with additional customizations
- Used for advanced testing and validation
- Highlights flexibility and extensibility

---
## Deployment Template / Design

- **Design Pattern:** M-Standard-MR
- **Deployment Type:** middle_rack
- **Maximum Super Spines:** 2
- **Maximum Spines:** 2
- **Maximum Pods:** 2
- **Maximum Leafs:** 16
- **Maximum Rack Leafs:** 6
- **Maximum Middle Racks:** 4
- **Maximum ToRs:** 24
- **Naming Convention:** standard

---
## Used Hardware Components

### Super Spines
- Dell PowerSwitch S5232F-ON (Template: PowerSwitch-S5232F-ON_SUPER_SPINE)

### Pods & Spines
- Pod 1: Cisco N9K-C9336C-FX2 (Template: N9K_C9336C_FX2_SPINE, 2 spines)
- Pod 2: Arista DCS-7050PX4-32S-R (Template: DCS-7050PX4-32S-R_SPINE, 2 spines)

### Racks & Leafs
- Pod 1 Racks:
	- Rack-1-1: Arista Leafs (Template: 4_ARISTA_LEAFS_DCS-7050SX3-24YC4C-S-R)
	- Rack-1-2: Dell Leafs (Template: 4_DELL_LEAFS_PowerSwitch-S5248F-ON)
	- Rack-1-3: Edgecore Leafs (Template: 4_EDGECORE_LEAFS_Edgecore-7AS7326-56X-O-48V-F)
- Pod 2 Racks:
	- Rack-2-1: Cisco Leafs (Template: 4_CISCO_LEAFS_N9K-C9336C-FX2)
	- Rack-2-2: Edgecore Leafs (Template: 2_EDGECORE_LEAFS_Edgecore-7AS7326-56X-O-48V-F)
	- Rack-2-3: Dell Leafs (Template: 4_DELL_LEAFS_PowerSwitch-S5248F-ON)

### Suites
- Suite-1: Racks 1-x (Arista, Dell, Edgecore)
- Suite-2: Racks 2-x (Cisco, Edgecore, Dell)

---
## Topology Overview

- 2 pods, each with dedicated spine hardware
- Each pod contains 3 racks with mixed leaf hardware
- Suites group racks by pod and vendor/hardware type
