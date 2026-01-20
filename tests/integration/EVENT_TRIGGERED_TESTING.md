# Event-Triggered Generator Testing Architecture

## Overview

Scenarios 02-05 test **event-triggered automation** where generators run automatically when specific data changes occur, rather than being manually invoked. This mirrors real-world GitOps workflows where infrastructure changes trigger automated provisioning.

## Event Architecture

### Event Components

1. **Generator Definitions** (`.infrahub.yml`):
   - `add_dc` - Data Center generator
   - `add_pod` - Pod generator
   - `add_rack` - Rack generator
   - `add_endpoint` - Endpoint connectivity generator

2. **Generator Actions** (`data/events/98_generator_action.yml`):
   - `run-dc-generator`
   - `run-pod-generator`
   - `run-rack-generator`
   - `run-endpoint-generator`

3. **Trigger Rules** (`data/events/99_actions.yml`):
   - Node mutation triggers (create/update)
   - Attribute match triggers
   - Relationship match triggers

## Scenario Event Mappings

### Scenario 1: DC Deployment (Initial Setup)
**Trigger**: Manual (bootstrap scenario)
- Creates DC1 with 3 pods and initial racks
- **No event trigger** - full topology created in one operation
- Pods and racks are created as nested children in DC definition

### Scenario 2: Add Switches (Rack Addition)
**Event**: `trigger-rack-generator-update-checksum`
**Trigger**: `LocationRack` object **updated** (checksum attribute)

**Flow**:
1. Load data: `02_switches/01_switches.yml` creates `LocationRack` with `fabric_templates`
2. **Event trigger**: Rack creation/update → runs `add_rack` generator
3. Generator creates: Leaf + ToR devices based on fabric templates
4. Generator cables: Devices to existing spines
5. Test verifies: Event-triggered generator completed

**Key verification points**:
- Each ToR has 2 interfaces (uplinks to spines)
- Each ToR has 2 cable connections to spine devices
- Devices have correct roles ("tor", "leaf")

### Scenario 3: Add Rack (Another Rack)
**Event**: `trigger-rack-generator-update-checksum`
**Trigger**: `LocationRack` object **updated** (checksum attribute)

**Flow**: Same as Scenario 2, different rack position (row 3 vs row 1)

### Scenario 4: Add Pod
**Event**: `trigger-pod-generator-update-checksum`
**Trigger**: `TopologyPod` object **updated** (checksum attribute)

**Flow**:
1. Load data: `04_pod/` creates POD-4 with Suite-4 and 2 initial racks
2. **Event trigger**: Pod update → runs `add_pod` generator
3. Generator creates: 2 spine switches
4. Generator cables: Spines to DC1's super-spines
5. **Cascade trigger**: Initial racks trigger `add_rack` generator
6. Rack generator creates: Leaf + ToR devices per rack
7. Test verifies: Pod generator + cascaded rack generators completed

**Key verification points**:
- POD-4 has 2 spine devices
- Each spine connected to 2 super-spines
- Initial racks have devices created (4 leafs + 4 tors)
- All cabling complete

### Scenario 5: Endpoint Connectivity
**Event**: `trigger-endpoint-generator-on-group-membership`
**Trigger**: `DcimDevice` object **updated** (member_of_groups relationship)

**Flow**:
1. Load data: `05_endpoint_connectivity/` creates compute racks
2. Load data: `01_devices.yml` creates servers with `member_of_groups: [endpoints]`
3. **Event trigger**: Device added to "endpoints" group → runs `add_endpoint` generator
4. Generator matches: Server interfaces to ToR downlink ports (speed-aware)
5. Generator creates: Dual-homed cables (each server → 2 ToRs)
6. Test verifies: Event-triggered endpoint connectivity

**Key verification points**:
- Each server has 4 interfaces (2x 100G or 4x 25G)
- Each server has 2 cable connections (dual-homed)
- Speed matching correct (100G↔100G, 25G↔25G)
- Servers connected to ToR devices in same pod

## Why Auto-Trigger is Commented Out for Creation

From `99_actions.yml`:

```yaml
# Auto-trigger for pod generator on creation commented out to
# prevent race conditions. Pod updates are still active and safe

# Auto-trigger for rack generator on creation commented out to
# prevent race conditions. Rack updates are still active and safe
```

**Race Condition**: When DC generator creates pods, it also sets checksums. If creation triggers fire immediately, both the DC generator (parent) and pod generator (child) run simultaneously, causing conflicts.

**Solution**:
- Creation triggers disabled for pods/racks
- Update triggers enabled (checksum updates)
- Generators explicitly update checksums after creating objects to trigger cascades safely

## Test Pattern for Event-Triggered Scenarios

```python
@pytest.mark.order(X01)
def test_01_load_data(client, branch):
    """Load data that will trigger event."""
    # Create branch
    # Load object files
    # Objects are created → events fire → generators run automatically

@pytest.mark.order(X02)
async def test_02_wait_for_generator(client, branch):
    """Wait for event-triggered generator to complete."""
    # Poll for generator tasks using CoreGeneratorRun
    # Wait for task state == COMPLETED
    # Verify no errors

@pytest.mark.order(X03)
async def test_03_verify_artifacts(client, branch):
    """Verify generator created expected objects."""
    # Verify devices created
    # Verify cables created
    # Verify detailed attributes (interfaces, roles, connections)

@pytest.mark.order(X04-X07)
def test_04_proposed_change_workflow(client, branch):
    """Standard PC workflow: create → validate → merge → verify."""
    # Rest of workflow same as Scenario 1
```

## Testing Checklist

For each scenario 02-05, verify:

- [ ] **Event Trigger Defined**: Check `99_actions.yml` for trigger rule
- [ ] **Generator Action Exists**: Check `98_generator_action.yml`
- [ ] **Data Triggers Event**: Loaded data causes mutation that matches trigger
- [ ] **Test Waits for Generator**: Test polls for task completion (not manual invocation)
- [ ] **Detailed Assertions**: Test verifies specific attributes/relationships
- [ ] **Idempotency**: Re-running scenario doesn't create duplicates

## Debugging Event Triggers

If generator doesn't run automatically:

1. **Check trigger rule matches**:
   - `branch_scope` = "other_branches" (not main)
   - `node_kind` matches object type
   - `mutation_action` matches (created/updated)
   - Attribute/relationship matches conditions

2. **Query generator run tasks**:
```graphql
query GetGeneratorRuns {
  CoreGeneratorRun(branch__name: "scenario-branch") {
    edges {
      node {
        id
        created_at
        state
        logs
        generator {
          name
        }
      }
    }
  }
}
```

3. **Check event logs** in Infrahub UI → Events tab

## Best Practices

1. **Always load to non-main branches** - Events only fire on `other_branches`
2. **Include group memberships** - Generators use groups for targeting (e.g., `topologies_rack`, `endpoints`)
3. **Wait for task completion** - Don't assume synchronous execution
4. **Test idempotency** - Verify re-running doesn't break or duplicate
5. **Cascade awareness** - Understand parent→child trigger chains (DC→Pod→Rack)
