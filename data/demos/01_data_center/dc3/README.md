
# Scenario: dc3

**Description:**
Medium data center - London

- Balanced topology for mid-size operations
- Good mix of scalability and cost
- Demonstrates typical enterprise deployment

---

## Deployment Template / Design
- **Design Pattern:** S-Flat-ToR
- **Template Source:** `07_dc_templates.yml`
- **Key Parameters:**
	- maximum_super_spines: 2
	- maximum_spines: 2
	- maximum_pods: 2
	- maximum_leafs: 0
	- maximum_rack_leafs: 0
	- maximum_middle_racks: 0
	- maximum_tors: 16
	- naming_convention: flat
	- deployment_type: tor

## Used Components
- **Super Spine:** PowerSwitch-S5232F-ON_SUPER_SPINE
- **Spine:** PowerSwitch-S5232F-ON_SPINE
- **Leafs:** PowerSwitch-S5224F-ON, PowerSwitch-S5248F-ON (in racks)
- **Racks:** Multiple network racks (see `02_racks.yml`)
- **Suites:** Room-1, Room-2 (see `01_suites.yml`)
- **Pods:** 2 pods, each with 2 spines

---

For full topology and configuration, see the YAML files in this folder.
