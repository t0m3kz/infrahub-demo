# Integration Tests

This directory contains integration tests that validate the complete Infrahub workflow for datacenter infrastructure management.

## Test Structure

The integration tests follow an **incremental scenario-based approach** that mirrors real-world operations:

### Test Phases

#### Phase 1: Foundation Setup (Order 1-7)
- `test_01_setup.py` - Schema loading, menu, and bootstrap data
- `test_02_repository.py` - Git repository integration and sync

#### Phase 2: Incremental Scenarios (Order 10+)
Each scenario creates infrastructure incrementally and merges to main:

**Workflow Pattern for Each Scenario:**
1. **Load Data** → Load scenario-specific data on a new branch
2. **Run Generator** → Execute appropriate generator (add_dc, add_rack, add_pod, etc.)
3. **Verify Artifacts** → Confirm devices, cabling, and configurations were created
4. **Create Proposed Change** → Create PC with diff
5. **Wait for Validations** → Ensure all checks pass
6. **Merge to Main** → Merge changes to main branch
7. **Verify in Main** → Confirm merged data exists in main

**Scenario Tests:**
- `test_10_dc_deployment.py` - **Scenario 1:** Initial datacenter deployment
- `test_20_add_switches.py` - **Scenario 2:** Add switches to existing DC
- `test_30_add_rack.py` - **Scenario 3:** Add new rack to existing pod
- `test_40_add_pod.py` - **Scenario 4:** Add new pod to existing DC
- `test_50_endpoint_connectivity.py` - **Scenario 5:** Add endpoint servers across deployment types

### Shared Utilities

#### `workflow_helpers.py` - Reusable Workflow Functions
- `run_generator()` - Run generator and wait for completion
- `verify_devices_created()` - Verify device creation with type breakdown
- `verify_cables_created()` - Verify cabling was created
- `create_proposed_change()` - Create PC with diff
- `wait_for_validations()` - Wait for validation checks
- `merge_proposed_change()` - Merge PC to main
- `verify_merged_to_main()` - Verify object exists in main branch

#### `test_helpers.py` - Generic Async Utilities
- `wait_for_condition()` - Polling utility for async conditions

#### Other Files
- `test_constants.py` - Timeout and delay constants
- `conftest.py` - Pytest fixtures and base test class
- `git_repo.py` - Git repository utilities (deprecated - use `infrahub_sdk.testing.repository.GitRepo`)

## Test Data

Test data is organized by scenario in `tests/integration/data/`:

```
data/
├── 01_dc/          # Scenario 1: Initial DC topology
├── 02_switches/    # Scenario 2: Switch additions
├── 03_racks/       # Scenario 3: New rack additions
├── 04_pod/         # Scenario 4: New pod additions
└── 05_endpoint_connectivity/  # Scenario 5: Endpoint servers in various deployments
```

## Running Tests

### Run All Integration Tests
```bash
uv run pytest tests/integration/ -v
```

### Run Setup Only
```bash
uv run pytest tests/integration/test_01_setup.py tests/integration/test_02_repository.py -v
```

### Run Specific Scenario
```bash
# Run only DC deployment scenario
uv run pytest tests/integration/test_10_dc_deployment.py -v

# Run DC + switches scenarios
uv run pytest tests/integration/test_10_dc_deployment.py tests/integration/test_20_add_switches.py -v
```

### Run with Explicit Ordering
```bash
uv run pytest tests/integration/ -v --order-scope=session
```

## Scenario Test Template

Each scenario follows this consistent pattern:

```python
class TestScenarioName(TestInfrahubDockerWithClient):
    """Test scenario description."""

    @pytest.fixture(scope="class")
    def scenario_branch(self) -> str:
        """Branch name for this scenario."""
        return "scenario-XX-description"

    @pytest.mark.order(100)  # Unique order number
    @pytest.mark.dependency(name="scenarioXX_load", depends=["previous_scenario"])
    def test_01_load_data(self, client_main, scenario_branch):
        """Load scenario data on branch."""
        # Create branch
        # Load data files
        pass

    @pytest.mark.order(101)
    @pytest.mark.dependency(name="scenarioXX_generator", depends=["scenarioXX_load"])
    @pytest.mark.asyncio
    async def test_02_run_generator(self, async_client_main, scenario_branch, workflow_state):
        """Run generator for scenario."""
        result = await run_generator(
            client=async_client_main,
            generator_name="add_dc",  # or add_rack, add_pod, etc.
            node_ids=[node_id],
            branch=scenario_branch,
        )
        workflow_state["scenarioXX_task"] = result
        pass

    @pytest.mark.order(102)
    @pytest.mark.dependency(name="scenarioXX_verify_devices", depends=["scenarioXX_generator"])
    @pytest.mark.asyncio
    async def test_03_verify_devices(self, async_client_main, scenario_branch):
        """Verify devices were created."""
        result = await verify_devices_created(
            client=async_client_main,
            branch=scenario_branch,
            expected_min_count=1,
            device_types=["spine", "leaf"],
        )
        pass

    @pytest.mark.order(103)
    @pytest.mark.dependency(name="scenarioXX_verify_cables", depends=["scenarioXX_generator"])
    @pytest.mark.asyncio
    async def test_04_verify_cables(self, async_client_main, scenario_branch):
        """Verify cabling was created."""
        result = await verify_cables_created(
            client=async_client_main,
            branch=scenario_branch,
            expected_min_count=1,
        )
        pass

    @pytest.mark.order(104)
    @pytest.mark.dependency(name="scenarioXX_pc", depends=["scenarioXX_verify_devices", "scenarioXX_verify_cables"])
    def test_05_create_proposed_change(self, client_main, scenario_branch, workflow_state):
        """Create proposed change and wait for validations."""
        pc_id = create_proposed_change(
            client=client_main,
            name="Scenario X: Description",
            source_branch=scenario_branch,
        )
        wait_for_validations(client=client_main, pc_name="Scenario X: Description")
        workflow_state["scenarioXX_pc_id"] = pc_id
        pass

    @pytest.mark.order(105)
    @pytest.mark.dependency(name="scenarioXX_merge", depends=["scenarioXX_pc"])
    def test_06_merge_to_main(self, client_main, workflow_state):
        """Merge proposed change to main."""
        pc_id = workflow_state["scenarioXX_pc_id"]
        result = merge_proposed_change(client=client_main, pc_id=pc_id)
        assert result["success"], f"Merge failed: {result}"
        pass

    @pytest.mark.order(106)
    @pytest.mark.dependency(name="scenarioXX_complete", depends=["scenarioXX_merge"])
    @pytest.mark.asyncio
    async def test_07_verify_in_main(self, async_client_main):
        """Verify object exists in main branch after merge."""
        success = await verify_merged_to_main(
            client=async_client_main,
            expected_object_kind="TopologyDataCenter",
            expected_object_name="DC1",
        )
        assert success
        pass
```

## Fixtures

### Provided by conftest.py
- `infrahub_port` (class) - Infrahub server port
- `async_client_main` (class) - Async client on main branch
- `client_main` (class) - Sync client on main branch
- `workflow_state` (class) - Shared state dictionary across tests
- `default_branch` (class) - Default test branch name
- `remote_repos_dir` (class) - Git repository directory
- `cleanup_on_failure` (class, autouse) - Automatic branch cleanup on test failure

### Scenario-Specific
- `scenario_branch` - Defined in each scenario test class

## Best Practices

1. **✅ Use Dedicated Scenario Branches** - Each scenario creates its own branch to isolate changes
2. **✅ Verify Artifacts Before PC** - Always verify devices/cables before creating proposed change
3. **✅ Wait for Validations** - Ensure all validation checks complete successfully
4. **✅ Verify Main After Merge** - Confirm objects exist in main branch post-merge
5. **✅ Use Workflow Helpers** - Leverage shared functions for consistency
6. **✅ Sequence with Order Marks** - Use `@pytest.mark.order()` for execution order
7. **✅ Declare Dependencies** - Use `@pytest.mark.dependency()` to track test relationships
8. **✅ Log Progress Clearly** - Use structured logging with scenario markers

## Troubleshooting

### Test Failures
- **Container logs**: Check `.pytest-tmp/` directory
- **Infrahub logs**: Run `docker compose logs infrahub-server`
- **Branch state**: Inspect via GraphQL UI at `http://localhost:8000/graphql`
- **Task details**: Check `workflow_state` for task IDs and results

### Branch Cleanup
Failed tests automatically trigger branch cleanup via the `cleanup_on_failure` fixture.

### Timeout Adjustments
Modify constants in `test_constants.py`:
```python
GENERATOR_TASK_TIMEOUT = 1800  # 30 minutes
MERGE_TASK_TIMEOUT = 600       # 10 minutes
VALIDATION_MAX_ATTEMPTS = 30   # 30 attempts × 30s = 15 minutes
```

## Migration from Legacy Tests

**Legacy monolithic test** (`test_worflow.py`):
- ❌ Single test file with all steps
- ❌ Single branch for all operations
- ❌ Merge everything at once
- ❌ Hard to debug failures
- ❌ Doesn't mirror real workflows

**New scenario-based tests**:
- ✅ Modular test files by scenario
- ✅ Each scenario has its own branch
- ✅ Incremental merges after verification
- ✅ Easy to debug specific scenarios
- ✅ Mirrors real-world operations

The legacy test has been renamed to `test_worflow_deprecated.py.bak` and should not be used.

## Quick Reference

### Common Commands
```bash
# Run everything
uv run pytest tests/integration/ -v

# Run foundation only
uv run pytest tests/integration/test_01_setup.py tests/integration/test_02_repository.py -v

# Run specific scenario
uv run pytest tests/integration/test_10_dc_deployment.py -v

# Run with dependencies
uv run pytest tests/integration/ -v --dependency

# Run with explicit ordering
uv run pytest tests/integration/ -v --order-scope=session

# Run and show logs
uv run pytest tests/integration/ -v -s
```

### Key Workflow Helper Functions
```python
# Run generator
result = await run_generator(client, "add_dc", [node_id], branch)

# Verify devices
result = await verify_devices_created(client, branch, expected_min_count=1, device_types=["spine", "leaf"])

# Verify cables
result = await verify_cables_created(client, branch, expected_min_count=1)

# Create PC + diff
pc_id = create_proposed_change(client, "PC Name", source_branch)

# Wait for validations
wait_for_validations(client, "PC Name")

# Merge PC
result = merge_proposed_change(client, pc_id)

# Verify in main
success = await verify_merged_to_main(client, "TopologyDataCenter", "DC1")
```
