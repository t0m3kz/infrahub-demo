# Test Scenario 4: Add New Pod to Existing Data Center

This scenario demonstrates adding a new pod (POD-4) to an existing data center (DC1).

## Purpose

Tests the `add_pod` generator's ability to:
- Add a new pod to an existing DC infrastructure
- Create spine switches for the new pod
- Establish super-spine to spine connectivity
- Maintain idempotency when run multiple times

## Data Files

- **00_suite.yml**: Suite-4 definition
- **01_pod.yml**: POD-4 topology definition
- **02_racks.yml**: Initial 2 network racks for POD-4

## Pod Configuration

- **Suite**: Suite-4 (ktw-1-s-4)
- **Parent**: DC1 (created in scenario 1)
- **Index**: 4
- **Deployment Type**: middle_rack (compute and storage, no direct server connections)
- **Spines**: 2x N9K-C9364C-GX_SPINE
- **Rows**: 3
- **Max Leafs per Row**: 3
- **Max ToRs per Row**: 0 (middle_rack deployment)
- **Initial Racks**: 2 network racks (row 1 & 2, position 1) with leaf+tor templates

## Expected Generator Behavior

The add_pod generator will:
1. Create Suite-4 (ktw-1-s-4)
2. Create pod "DC1-1-POD-4"
3. Create 2 spine switches
4. Cable spines to DC1's 2 super-spines
5. Create 2 network racks with fabric devices (4 leafs + 4 tors total)
6. Cable leafs to spines and tors to leafs
7. Update deployments and relationships

## Dependencies

- Scenario 1 (DC deployment) must complete successfully
- DC1 with super-spines must exist

## Load Command

```bash
uv run infrahubctl object load tests/integration/data/04_pod/ --branch <branch-name>
```
