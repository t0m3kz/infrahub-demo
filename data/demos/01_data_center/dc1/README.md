
# Scenario: dc1

**Description:**
Large data center - Paris (Multiple Pods)

- Multiple pods for high scalability
- Suitable for large enterprise or cloud deployments
- Demonstrates full topology with redundancy and scale

---

## Deployment Template / Design
- **Design Pattern:** L-Hierarchical-MR
- **Template Source:** `07_dc_templates.yml`
- **Key Parameters:**
	- maximum_super_spines: 4
	- maximum_spines: 4
	- maximum_pods: 4
	- maximum_leafs: 24
	- maximum_rack_leafs: 8
	- maximum_middle_racks: 8
	- maximum_tors: 48
	- naming_convention: hierarchical
	- deployment_type: middle_rack

## Used Components
- **Super Spine:** N9K_C9336C_FX2_SUPER_SPINE
- **Spine:** N9K-C9364C-GX_SPINE
- **Leafs:** N9K-C9336C-FX2 (in racks)
- **Racks:** Multiple network racks (see `02_racks.yml`)
- **Suites:** Suite-1, Suite-2, Suite-3, Suite-4 (see `01_suites.yml`)
- **Pods:** 4 pods, each with 2-4 spines

---

For full topology and configuration, see the YAML files in this folder.
