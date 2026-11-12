# Test Scenario 4: Add New Pod to Existing Data Center

Three pods were plenty, right up until the fourth one was approved.

This scenario demonstrates adding a new pod (POD-4) to the existing DC6 data center, the networking equivalent of
adding a room to a house without asking the neighbours.

## Purpose

Tests the `add_pod` generator's ability to:

- Add a new pod to an existing DC infrastructure
- Create spine switches for the new pod
- Establish super-spine to spine connectivity
- Maintain idempotency when run multiple times

## Data Files

- **00_suite.yml**: Suite-4 definition, the room the pod will live in
- **01_pod.yml**: POD-4 topology definition
- **02_racks.yml**: Initial 2 network racks for POD-4

## Pod Configuration

- **Suite**: Suite-4 (ktw-1-s-4)
- **Parent**: DC6 (created in the DC6 scenario)
- **Index**: 4
- **Deployment Type**: middle_rack (compute and storage, no direct server connections)
- **Spines**: 2x N9K-C9336C-FX2_SPINE
- **Design**: M_MIDDLE (`TopologyTemplatePod`: rows, racks per row, spines per pod)
- **Rows**: 4 rows of 9 rack slots (suite ktw-1-s-4 `rows` / `racks_per_row`, the M_MIDDLE design envelope); the initial racks use 2 of them
- **ToRs**: none (middle_rack deployment)
- **Initial Racks**: 2 network racks (row 1 & 2, position 1), each with 2 leafs + 2 access leafs
- **Leaf / access-leaf templates**: N9K-C9336C-FX2_LEAF_MR + N9K-C93180YC-EX_ACCESS (NX-OS)
- **MLAG**: `virtual` (vPC fabric peering). SONiC MC-LAG needs a physical peer-link, and access-leaf templates
  have no mlag-peer ports, so this pod stays on NX-OS

## Expected Generator Behavior

The add_pod generator will, in this order:

1. Create Suite-4 (ktw-1-s-4)
2. Create pod "DC6-1-POD-4"
3. Create 2 spine switches
4. Cable spines to DC6's 2 super-spines
5. Create 2 network racks with fabric devices (4 leafs + 4 access leafs total)
6. Cable leafs to spines and access leafs to leafs
7. Update deployments and relationships

## Dependencies

- The DC6 deployment must complete successfully
- DC6 with super-spines must exist

## Load Command

As a reviewed change, with DC6 and the earlier growth steps first: `uv run invoke kick dc6-growth --from-stage pod`.
Or by hand:

```bash
uv run infrahubctl object load data/demos/04_pod_dc6/ --branch <branch-name>
```

## Validation

Use `uv run invoke kick dc6-growth` (this pod is its `pod` stage) to validate the automatic DC/POD/rack cascade. The `pod` stage of the integration suite's `dc6-suite` kick asserts it.
