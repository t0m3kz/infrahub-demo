
# Scenario: dc2

**Description:**
Small-Medium data center - Frankfurt

- Compact topology for smaller footprint
- Ideal for regional or branch deployments
- Focuses on cost efficiency and simplicity

---

## Deployment Template / Design
- **Design Pattern:** M-Standard-MR
- **Template Source:** `07_dc_templates.yml`
- **Key Parameters:**
	- maximum_super_spines: 2
	- maximum_spines: 2
	- maximum_pods: 2
	- maximum_leafs: 16
	- maximum_rack_leafs: 6
	- maximum_middle_racks: 4
	- maximum_tors: 24
	- naming_convention: standard
	- deployment_type: middle_rack

## Used Components
- **Super Spine:** DCS-7050CX3-32C-R_SUPER_SPINE
- **Spine:** DCS-7050PX4-32S-R_SPINE, DCS-7050CX3-32C-R_SPINE
- **Leafs:** DCS-7050CX3-32C-R, DCS-7050CX4M-48D8-F, DCS-7050SX3-24YC4C-S-R (in racks)
- **Racks:** Multiple network racks (see `02_racks.yml`)
- **Suites:** Suite-1, Suite-2 (see `01_suites.yml`)
- **Pods:** 2 pods, each with 2 spines

---

For full topology and configuration, see the YAML files in this folder.
