# tests/AGENTS.md

pytest with async auto-mode enabled.

## Commands

```bash
uv run pytest tests/unit/                    # Unit tests (fast, mocked)
uv run pytest tests/integration/             # Integration tests (real Infrahub)
uv run pytest -n 4                           # Parallel execution
uv run pytest tests/unit/test_client.py      # Single file
```

## Structure

```text
tests/
├── unit/           # Fast, mocked, no external deps
├── integration/    # Real Infrahub via testcontainers
└── smoke/        # Test using Infrahub plugins
```

## Test Patterns

```python
# Async test - NO decorator needed (auto mode)
async def test_async_operation(httpx_mock: HTTPXMock):
    httpx_mock.add_response(
        url="http://localhost:8000/api/graphql",
        json={"data": {"result": "success"}},
    )
    client = InfrahubClient()
    result = await client.execute(query="...")
    assert result is not None

# Sync test
def test_sync_operation():
    client = InfrahubClientSync()
    # ...

# CLI test
def test_cli_command():
    runner = CliRunner()
    result = runner.invoke(app, ["command", "--flag"])
    assert result.exit_code == 0
```

## Boundaries

✅ **Always**

- Use `httpx_mock` fixture for HTTP mocking
- Clean up resources in integration tests
- Test primary execution paths
- Verify error handling for missing data

🚫 **Never**

- Add `@pytest.mark.asyncio` (globally enabled)
- Make unit tests depend on external services
- Skip testing error scenarios

## Generator Testing Best Practices

```python
# Generator test with httpx_mock
async def test_generator_operation(httpx_mock: HTTPXMock):
    # Mock API responses
    httpx_mock.add_response(
        url="http://localhost:8000/api/graphql",
        json={"data": {"Result": {"edges": [{"node": {"id": "1"}}]}}},
    )

    # Initialize and test
    client = InfrahubClient()
    generator = MyGenerator(client=client, query="query", infrahub_node=Mock())
    await generator.generate(data={"key": "value"})

    # Assertions
    assert generator.data is not None

# Test strategy methods independently
async def test_primary_strategy(httpx_mock: HTTPXMock):
    """Test primary execution path with valid data."""
    # Setup mocks for primary path
    # Verify primary logic executes correctly
    pass

async def test_error_handling(httpx_mock: HTTPXMock):
    """Test error handling when required data is missing."""
    # Setup mocks with missing data
    # Verify appropriate error raised or logged
    pass

# Test idempotency
async def test_operation_idempotency(httpx_mock: HTTPXMock):
    """Verify operation produces consistent results on multiple runs."""
    # Execute operation twice with same inputs
    # Assert no duplicates or conflicts created
    pass
```

## AI Agent Test Creation Guidelines

When writing tests for InfraHub components:

1. **Isolate concerns**: Test each method/strategy independently
2. **Mock external calls**: Use `httpx_mock` for all HTTP/GraphQL requests
3. **Test execution paths**: Success and error scenarios
4. **Verify idempotency**: Ensure repeated operations are safe
5. **Test edge cases**: Empty data, missing fields, type mismatches
6. **Use clear names**: `test_connects_when_target_available`
7. **Single focus**: One logical behavior per test
8. **Document intent**: Docstrings explaining tested behavior

### Recommended Test Organization

```python
class TestComponentBehavior:
    """Test primary component functionality."""

    async def test_successful_operation(self, httpx_mock: HTTPXMock):
        """Happy path: operation succeeds with valid input."""
        pass

    async def test_handles_missing_data(self, httpx_mock: HTTPXMock):
        """Error handling: gracefully handles missing required data."""
        pass

class TestDataValidation:
    """Test input validation and type checking."""

    async def test_validates_required_fields(self, httpx_mock: HTTPXMock):
        """Validation: rejects data missing required fields."""
        pass

    async def test_enforces_type_constraints(self, httpx_mock: HTTPXMock):
        """Type safety: validates field types match expectations."""
        pass
```