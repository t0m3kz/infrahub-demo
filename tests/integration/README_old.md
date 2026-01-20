# Integration Tests

This directory contains integration tests for the infrahub-demo project. The tests validate the complete workflow from schema loading to merge verification.

## Test Structure

The integration tests are organized into modules by workflow phase:

### Test Modules

1. **test_01_setup.py** - Schema and bootstrap data loading
   - Load base schemas
   - Load extension schemas
   - Load menu definitions
   - Load bootstrap data
   - Load event definitions

2. **test_02_repository.py** - Repository management
   - Add Git repository to Infrahub
   - Wait for repository sync
   - Verify repository content availability

3. **test_03_branch.py** - Branch operations
   - Create new branch
   - Wait for branch endpoint to be ready
   - Load branch-specific data (DC1 demo)
   - Verify data loaded correctly

4. **test_04_generator.py** - Generator execution
   - Run datacenter generator (add_dc)
   - Wait for generator completion
   - Verify generated devices

5. **test_05_proposed_change.py** - Proposed changes workflow
   - Create diff for branch
   - Create proposed change
   - Wait for validations
   - Merge proposed change

6. **test_06_verification.py** - Merge verification
   - Verify DC1 exists in main branch
   - Verify devices exist in main branch
   - Final integrity checks

### Supporting Modules

- **conftest.py** - Pytest fixtures and base test classes
  - `TestInfrahubDockerWithClient` - Base class with Docker container and clients
  - `async_client_main` - Async Infrahub client fixture
  - `client_main` - Sync Infrahub client fixture
  - `workflow_state` - Shared state across tests
  - `default_branch` - Default branch name
  - `cleanup_on_failure` - Auto-cleanup on test failure

- **test_helpers.py** - Shared utility functions
  - `wait_for_condition()` - Polling helper for async conditions

- **test_constants.py** - Timeout and polling constants
  - All timeout values in one place for easy tuning

- **git_repo.py** - Git repository helper (deprecated, use SDK version)

## Running Tests

### Run all integration tests

```bash
pytest tests/integration/ -v
```

### Run specific test module

```bash
pytest tests/integration/test_01_setup.py -v
pytest tests/integration/test_04_generator.py -v
```

### Run with dependency tracking

```bash
pytest tests/integration/ -v --dependency-graph
```

### Run with logs

```bash
pytest tests/integration/ -v --log-cli-level=INFO
```

## Test Dependencies

Tests use `pytest-dependency` to ensure proper execution order:

```text
schema_load
  → schema_extensions
    → menu_load
    → bootstrap_data
      → events_data
        → add_repository
          → repository_sync
            → create_branch
              → branch_endpoint_ready
                → load_dc_design
                  → verify_dc_created
                    → run_generator
                      → generator_complete
                        → verify_devices_created
                          → create_diff
                            → create_proposed_change
                              → wait_validations
                                → merge_proposed_change
                                  → verify_merge_to_main
                                    → verify_devices_in_main
```

## Test Data

Integration test data is located in `tests/integration/data/`:

- `01_dc/` - DC1 datacenter deployment data
- `02_switches/` - Switch configuration data
- `03_racks/` - Rack deployment data
- `04_pod/` - Pod topology data
- `endpoint_connectivity/` - Endpoint connectivity data

## Configuration

### Timeouts

All timeouts are defined in `test_constants.py`:

- `REPO_SYNC_MAX_ATTEMPTS` = 60 (10 minutes)
- `GENERATOR_TASK_TIMEOUT` = 1800 (30 minutes)
- `DIFF_TASK_TIMEOUT` = 600 (10 minutes)
- `MERGE_TASK_TIMEOUT` = 600 (10 minutes)
- `VALIDATION_MAX_ATTEMPTS` = 30 (15 minutes)

### Branch Name

Default test branch is `add-dc1`, defined in `conftest.py:default_branch()` fixture.

## Workflow State

Tests share state via the `workflow_state` fixture:

```python
workflow_state = {
    "pc_id": None,              # Proposed change ID
    "dc_id": None,              # Datacenter ID
    "repository_id": None,      # Repository ID
    "generator_task_id": None,  # Generator task ID
    "generator_task_state": None,  # Generator task state
}
```

This allows later tests to reference objects created by earlier tests.

## Best Practices

### Test Isolation

- Each test module is independent but depends on previous modules
- Use `workflow_state` fixture to pass data between tests
- Tests clean up on failure via `cleanup_on_failure` fixture

### Error Handling

- All tests include detailed error messages with context
- Failed tests log diagnostic information
- Container logs are captured on failure

### Assertions

- Use descriptive assertion messages with multi-line strings
- Include relevant IDs, states, and context in error messages
- Log progress at INFO level for visibility

### Async/Sync

- Use async tests (`@pytest.mark.asyncio`) for Infrahub API calls
- Use sync tests for CLI commands (`execute_command`)
- Use `await asyncio.sleep()` for data propagation delays

## Troubleshooting

### Tests Fail at Repository Sync

- Check that repository mount is correctly configured
- Verify `.pytest_cache` and `.venv` are excluded from repository copy
- Increase `REPO_SYNC_MAX_ATTEMPTS` if needed

### Generator Timeout

- Check generator logs in Infrahub container
- Increase `GENERATOR_TASK_TIMEOUT` for large topologies
- Verify DC1 data was loaded correctly in test_03

### Merge Failures

- Check validation results in test_03 output
- Verify all validations completed (even if some failed)
- Check proposed change state before merge
- Review merge task logs for detailed error messages

### Data Not Found in Main

- Ensure `MERGE_PROPAGATION_DELAY` is sufficient
- Check that merge completed successfully (PC state = "merged")
- Verify no errors in merge task logs

## Maintenance

### Adding New Tests

1. Create new test module following naming convention `test_XX_<name>.py`
2. Update order numbers to fit in workflow sequence
3. Add dependencies using `@pytest.mark.dependency`
4. Update this README with new test module description

### Modifying Timeouts

Edit values in `test_constants.py` rather than hardcoding in tests.

### Updating Test Data

Test data files are in `tests/integration/data/`. Follow object file format:

```yaml
---
apiVersion: infrahub.app/v1
kind: Object
spec:
  kind: TopologyDataCenter
  data:
    - name: "DC1"
      # ...
```

## References

- [Infrahub Testing Documentation](https://docs.infrahub.app/python-sdk/topics/testing)
- [pytest-dependency](https://pytest-dependency.readthedocs.io/)
- [pytest-order](https://pytest-order.readthedocs.io/)
