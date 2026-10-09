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

- `test_10_dc_deployment.py` - **Scenario 1:** Initial datacenter deployment — every DC loads, generates and validates its proposed change at the same time on its own branch (`DC_DEPLOYMENT_CONCURRENCY` caps how many; unset means all), then the DCs merge one after another
- `test_12_dc6_add_switch.py` - **Scenario 2:** Add switches to an existing DC6 rack (`data/demos/02_switch_dc6`)
- `test_14_dc6_add_rack.py` - **Scenario 3:** Add a rack to an existing DC6 pod (`data/demos/03_rack_dc6`)
- `test_16_dc6_add_pod.py` - **Scenario 4:** Add a pod to DC6 (`data/demos/04_pod_dc6`)
- `test_18_dc6_add_spine.py` - **Scenario 5:** Add a spine to DC6-1-POD-1 by swapping its spine element
- `test_19_dc6_segments.py` - **Scenario 6:** Board C001 into DC6 and deploy two VXLAN segments (`data/20_segments`)
- `test_20_dc6_add_endpoints.py` - **Scenario 7:** Endpoint servers across DC6's pods (`data/demos/06_servers`)
- `test_59` - `test_65` - **Scenario 8:** The `30_all` demo, end to end (see [The 30_all Suite](#the-30_all-suite)). This one deviates from the pattern above: it loads in stages, shares one branch across six modules, and does not merge to main.

Scenarios 2-7 form one chain on DC6: `test_12` depends on `dc6_verify_after_merge` from `test_10`, and each
later module depends on the previous one's merge. Every growth check is scoped to DC6 (devices reachable from
the DC, its pods and their racks), and every pre-existing underlay switch must keep its ASN. Only DC6 has to be
deployed for the chain, so `DC_DEPLOYMENT_TEST_DCS=dc6` cuts `test_10` down to that one DC (see
[Run Specific Scenario](#run-specific-scenario)).

### Shared Utilities

#### `workflow_helpers.py` - Reusable Workflow Functions

- `run_generator()` - Run generator and wait for completion
- `verify_devices_created()` - Verify device creation with type breakdown
- `verify_cables_created()` - Verify cabling was created
- `create_proposed_change()` - Create PC with diff
- `wait_for_validations()` - Wait for validation checks
- `merge_proposed_change()` - Merge PC to main

#### `test_helpers.py` - Generic Async Utilities

- `wait_for_condition()` - Polling utility for async conditions

#### Other Files

- `test_constants.py` - Timeout and delay constants
- `conftest.py` - Pytest fixtures and base test class
- `git_repo.py` - Git repository utilities (deprecated - use `infrahub_sdk.testing.repository.GitRepo`)

## Test Data

Test data is organized by scenario in `tests/integration/data/`:

```text
data/
├── 20_segments/               # C001's DC6 footprint and two VXLAN segments (test_19)
└── 60_app_catalogue/          # Application catalogue and deployment requests
```

The DC6 chain (`test_12`-`test_20`) loads its switch, rack, pod and server data straight from `data/demos/`
(paths in `test_constants.py`), so the demos and the tests cannot drift apart. `20_segments` is the one
test-local exception: `data/demos/07_customers` and `08_segments` depend on the `30_all` customer organizations
and still use the old `[org_id, environment]` footprint HFID, so they cannot be loaded on their own.

The `30_all` suite (`test_59`-`test_65`) is the exception: it loads from `data/demos/30_all/` at the repository
root, not from here, because it exercises the shipped demo rather than test-only fixtures.

## Running Tests

### Run All Integration Tests

```bash
uv run invoke test-integration
```

Run them through Invoke rather than calling `pytest` directly. `_run_integration_suite` in `tasks.py` sets two
things bare `pytest` does not, and each one fails in a way that does not look like its cause:

| Set by the task | What happens without it |
| --- | --- |
| `INFRAHUB_TESTING_ENABLE_INTEGRATION=1` | Every test is **silently skipped**. A run reporting `16 skipped` tested nothing, and exits 0. |
| `--basetemp ~/.pytest-tmp/infrahub-demo` | On Docker Desktop the stack dies ~20s in, before collection. See below. |

**The basetemp trap.** `InfrahubDockerCompose.init()` writes `docker-compose.yml` and `haproxy.cfg` into pytest's
basetemp, and the `infrahub-server-lb` service bind-mounts `./haproxy.cfg`. pytest's default basetemp is under
`/private/var/folders/...`, which Docker Desktop does not share with its VM by default. Docker answers an
unshareable bind source by creating an **empty directory** at the mount point, so HAProxy is handed a directory
where its config should be, prints its usage text and exits 1 — and `up --wait` then aborts the whole stack with
every other service healthy. The symptom is `Failed to start docker compose ... returned non-zero exit status 1`
plus `infrahub-server-lb-1 exited (1)`, which reads like a port conflict and is not one. Confirm a path is
shareable with:

```bash
docker run --rm -v /tmp/somefile:/x --entrypoint sh haproxy:3.1-alpine \
  -c 'if [ -d /x ]; then echo "DIRECTORY (mount broken)"; else echo "file, $(wc -c </x) bytes"; fi'
```

`~/.pytest-tmp` sits under `/Users`, which Docker Desktop shares out of the box. Use the tasks, or add your
basetemp to Docker Desktop → Settings → Resources → File sharing.

The focused profiles reuse one Docker stack per command and keep the feedback loop from becoming a small infrastructure project of its own:

```bash
# Setup and repository prerequisites
uv run invoke test-integration-fast

# The 30_all demo, end to end
uv run invoke test-integration-all-demo

# Automatic DC/POD/rack routing regression
uv run invoke test-integration-routing

# Full integration matrix
uv run invoke test-integration
```

The fast profile intentionally stops after setup and repository synchronization. The routing profile adds one focused acceptance workflow. The full profile runs the complete scenario chain and is the right choice for release-level validation.

### The 30_all Suite

`test_59` through `test_65` are one scenario split across six modules. They share a single branch (`ALL_DEMO_BRANCH`) and run in dependency order:

| Module | Covers |
| --- | --- |
| `test_59_all_demo_load.py` | The staged load, no failed tasks, every trigger-dispatched generator, declared-object inventory |
| `test_60_app_catalogue.py` | Deployment-request materialization, proxy egress rule, inter-segment firewall rule |
| `test_61_all_demo_compute.py` | The three fabrics, their routing, host cabling, and the application graph down to hosting devices |
| `test_62_all_demo_interconnects.py` | Physical and virtual circuits, cloud terminations, firewall contexts, segment legs, which metro's firewalls serve each colocation deployment |
| `test_64_all_demo_firewall_config.py` | Colo cloud/partner/SaaS zone policies: served by the FR metro's firewall pair, `CheckFirewall` passes, every pair member renders them and a firewall outside the pair does not |
| `test_65_all_demo_idempotency.py` | Two more runs of the security generators, then of the topology generators, leave rules, contexts, devices, cables, addresses, pools and allocated numbers unchanged |

Only `test_59` loads data, and the load is deliberately staged — later stages reference objects that only exist once an earlier stage's generators have finished. `ALL_DEMO_LOAD_STAGES` in `tasks.py` (shared with `invoke load-all-demo`, re-exported by `test_constants.py`) documents which stage needs what. Running `test_60`-`test_65` on their own will skip: their session-scoped dependencies are unmet without `test_59`.

### Run Setup Only

```bash
uv run invoke test-integration-fast
```

### Run Specific Scenario

Pass a module list to `--tests`, keeping the setup pair in front — session-scoped dependencies mean a module run
without its prerequisites skips rather than fails:

```bash
# DC deployment scenario
uv run invoke test-integration --tests "tests/integration/test_01_setup.py \
  tests/integration/test_02_repository.py tests/integration/test_10_dc_deployment.py"

# DC6 + the whole DC6 scenario chain (switch, rack, pod, spine, segments, endpoints)
DC_DEPLOYMENT_TEST_DCS=dc6 uv run invoke test-integration --tests "tests/integration/test_01_setup.py \
  tests/integration/test_02_repository.py tests/integration/test_10_dc_deployment.py \
  tests/integration/test_12_dc6_add_switch.py tests/integration/test_14_dc6_add_rack.py \
  tests/integration/test_16_dc6_add_pod.py tests/integration/test_18_dc6_add_spine.py \
  tests/integration/test_19_dc6_segments.py tests/integration/test_20_dc6_add_endpoints.py"
```

The DC6 chain and the `30_all` suite can share a session. `test_19` merges C001's DC6 footprint and its
segments into main, so `test_62` scopes its exact firewall-context and segment-leg counts to the `30_all` DCs
(`scope_tenant_services_to_dcs`) rather than counting them across the whole instance.

`--server-port` moves the stack off the default 8100 if you need two runs side by side.

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

- **Generated compose project**: `~/.pytest-tmp/infrahub-demo/` — the compose file, `haproxy.cfg` and the logs
- **Infrahub logs**: `docker compose ls -a` to find the live `infrahub-test-*` project, then
  `docker logs <project>-infrahub-server-1`
- **Branch state**: the suite's own GraphQL UI is on the test port (8100 by default), not 8000
- **Task details**: Check `workflow_state` for task IDs and results
- **A stack that never becomes healthy**: on Docker Desktop this is almost always the basetemp/file-sharing trap
  described under [Run All Integration Tests](#run-all-integration-tests), not anything wrong with the tests.
  Work from the outside in: `docker compose ls -a` to find the project, then read *one* service's log rather than
  the conftest's dump of all of them

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
uv run invoke test-integration

# Run foundation only
uv run invoke test-integration-fast

# The 30_all demo
uv run invoke test-integration-all-demo

# One scenario, with its prerequisites in front
uv run invoke test-integration --tests "tests/integration/test_01_setup.py \
  tests/integration/test_02_repository.py tests/integration/test_10_dc_deployment.py"

# Two runs side by side
uv run invoke test-integration-fast --server-port 8200
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
```
