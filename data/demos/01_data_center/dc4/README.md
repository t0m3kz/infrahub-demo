
# Scenario: dc4

**Description:**
Medium-Large data center - Amsterdam

- Larger than typical mid-size, with extra redundancy
- Suitable for regional hubs or service providers
- Shows advanced features and failover

---

## Deployment Template / Design
- **Design Pattern:** L-Standard-Mixed
- **Template Source:** `07_dc_templates.yml`
- **Key Parameters:**
	- maximum_super_spines: 4
	- maximum_spines: 4
	- maximum_pods: 4
	- maximum_leafs: 32
	- maximum_rack_leafs: 8
	- maximum_middle_racks: 8
	- maximum_tors: 32
	- naming_convention: standard
	- deployment_type: mixed

## Used Components
- **Super Spine:** Edgecore-7726_32X_O_SUPER_SPINE
- **Spine:** Edgecore-7726_32X_O_SPINE
- **Leafs:** Edgecore-7AS7326-56X-O-48V-F (in racks)
- **Racks:** Multiple network racks (see `02_racks.yml`)
- **Suites:** Suite-1, Suite-2, Suite-3 (see `01_suites.yml`)
- **Pods:** 3 pods, each with 2-4 spines

---

For full topology and configuration, see the YAML files in this folder.
