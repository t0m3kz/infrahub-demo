# GitHub Copilot Instructions for InfraHub Demo

## Project Overview

This is the **infrahub-demo** project - a comprehensive demonstration of design-driven network automation using [InfraHub](https://docs.infrahub.app). The project showcases how to build scalable network infrastructure through schemas, generators, transforms, and validation checks.

## Package Manager & Dependencies

- **Package Manager**: Use `uv` for all dependency management
- **Python Version**: Supports Python 3.10, 3.11, or 3.12
- **Key Dependencies**: `infrahub-sdk[all]>=1.7.2,<2.0.0`, `invoke>=2.2.0`

### Common uv Commands
```bash
# Setup project
uv sync

# Install dev dependencies
uv sync --group dev

# Run commands
uv run pytest
uv run infrahubctl schema load schemas
uv run invoke start
```

## Project Architecture

### Directory Structure
```
├── checks/          # Validation logic for devices and configurations
├── data/            # Bootstrap data and demo scenarios
├── generators/      # Topology and infrastructure generators
├── menu/            # InfraHub menu definitions
├── queries/         # GraphQL queries for data retrieval
├── schemas/         # Base schemas and extensions
├── scripts/         # Automation scripts
├── templates/       # Jinja2 templates for device configs
├── tests/           # Unit and integration tests
└── transforms/      # Data transformation logic
```

### Core Components
1. **Schemas** - Define data models and relationships
2. **Generators** - Create topology and infrastructure
3. **Transforms** - Process data for device configurations
4. **Checks** - Validate configurations and connectivity
5. **Templates** - Generate device-specific configurations

## Development Patterns

### InfraHub SDK Usage

#### Generator Pattern
```python
from infrahub_sdk.generators import InfrahubGenerator

class MyTopologyGenerator(InfrahubGenerator):
    async def generate(self, data: dict) -> None:
        # Implementation here
        pass
```

#### Transform Pattern
```python
from infrahub_sdk.transforms import InfrahubTransform

class MyTransform(InfrahubTransform):
    query = "my_config_query"

    async def transform(self, data: Any) -> Any:
        # Process and return transformed data
        return processed_data
```

#### Check Pattern
```python
from infrahub_sdk.checks import InfrahubCheck

class MyCheck(InfrahubCheck):
    def validate(self, data: Any) -> None:
        # Validation logic
        if not valid_condition:
            self.log_error("Validation failed")
```

### Schema Development

#### Base vs Extensions
- **Base schemas** (`schemas/base/`): Core models (DCIM, IPAM, Location, Topology)
- **Extension schemas** (`schemas/extensions/`): Feature-specific extensions

#### Schema Structure
```yaml
nodes:
  - name: MyNode
    namespace: MyNamespace
    description: "Description of the node"
    inherit_from:
      - BaseNode
    attributes:
      - name: my_attribute
        kind: Text
        order_weight: 1000
    relationships:
      - name: my_relation
        peer: RelatedNode
        cardinality: many
```

#### Common Schema Patterns
- Use `inherit_from` for extending base functionality
- Set `order_weight` for UI field ordering
- Include `description` for documentation
- Use proper `namespace` organization
- Define `cardinality` for relationships (one/many)

### Naming Conventions

#### File Naming
- Schemas: `snake_case.yml`
- Python modules: `snake_case.py`
- Classes: `PascalCase`
- Functions/methods: `snake_case`

#### InfraHub Naming
- Nodes: `PascalCase` (e.g., `LocationBuilding`)
- Attributes: `snake_case` (e.g., `device_type`)
- Relationships: `snake_case` (e.g., `parent_location`)
- Namespaces: `PascalCase` (e.g., `Dcim`, `Ipam`, `Service`)

### Configuration Files

#### .infrahub.yml Structure
```yaml
# Define transformations
jinja2_transforms:
  - name: my_template
    query: my_query
    template_path: templates/my_template.j2

# Define artifacts
artifact_definitions:
  - name: my_config
    targets: my_targets
    transformation: my_transform

# Define checks
check_definitions:
  - name: validate_my_device
    class_name: MyCheck
    file_path: checks/my_check.py
    targets: my_devices

# Define Python transforms
python_transforms:
  - name: my_transform
    class_name: MyTransform
    file_path: transforms/my_transform.py

# Define generators
generator_definitions:
  - name: create_topology
    class_name: MyGenerator
    file_path: generators/my_generator.py
    targets: my_topologies
    query: my_topology_query

# Define queries
queries:
  - name: my_query
    file_path: queries/my_query.gql
```

## Testing Requirements

### Testing Strategy
- **Unit Tests**: Mock-based testing with `unittest.mock`
- **Integration Tests**: Full workflow validation
- **Every functionality MUST be tested**

### Test Structure
```python
# Unit test example
from unittest.mock import Mock, patch
import pytest

class TestMyComponent:
    @patch("pathlib.Path.exists")
    def test_component_functionality(self, mock_exists: Mock) -> None:
        mock_exists.return_value = True
        # Test implementation
        assert expected_result
```

### Test Fixtures
- Use `tests/conftest.py` for shared fixtures
- Mock external dependencies
- Test both success and failure scenarios
- Validate schema structures and data formats

### Running Tests
```bash
# Run all tests
uv run pytest

# Run with verbose output
uv run pytest -vv

# Run specific test file
uv run pytest tests/unit/test_my_component.py

# Run with coverage
uv run pytest --cov=. --cov-report=html
```

## Code Quality Standards

### Type Hints
- **REQUIRED**: All functions must have type hints
- Use `from typing import Any, Dict, List, Optional`
- Return type hints are mandatory

```python
from typing import Any, Dict, List

async def process_data(data: Dict[str, Any]) -> List[str]:
    """Process data and return list of strings."""
    return []
```

### Code Formatting
- **Tool**: `ruff` for formatting and linting
- **Configuration**: Uses project's `pyproject.toml` settings
- **Enforcement**: Run before committing

```bash
# Format code
uv run ruff check . --fix

# Type checking
uv run mypy .

# Run quality checks
uv run invoke validate
```

### Documentation
- Use docstrings for classes and functions
- Include parameter and return value descriptions
- Add inline comments for complex logic

```python
class MyGenerator(InfrahubGenerator):
    """Generate network topology based on design patterns.

    This generator creates devices, interfaces, and connections
    according to the specified topology design.
    """

    async def generate(self, data: dict) -> None:
        """Generate topology infrastructure.

        Args:
            data: Topology configuration data
        """
        # Implementation
```

## Common Development Tasks

### Creating a New Schema Extension

1. Create schema file in `schemas/extensions/my_feature/`
2. Define nodes with proper inheritance
3. Add to InfraHub configuration
4. Create test coverage

```yaml
# schemas/extensions/my_feature/my_schema.yml
nodes:
  - name: MyDevice
    namespace: MyNamespace
    inherit_from:
      - DcimGenericDevice
    attributes:
      - name: custom_attribute
        kind: Text
```

### Creating a Generator

1. Create Python file in `generators/`
2. Inherit from `InfrahubGenerator`
3. Implement `generate()` method
4. Add to `.infrahub.yml`
5. Create GraphQL query
6. Write tests

```python
# generators/my_generator.py
from infrahub_sdk.generators import InfrahubGenerator

class MyGenerator(InfrahubGenerator):
    async def generate(self, data: dict) -> None:
        # Generator logic
        pass
```

### Creating a Transform

1. Create Python file in `transforms/`
2. Inherit from `InfrahubTransform`
3. Define query attribute
4. Implement `transform()` method
5. Add to `.infrahub.yml`
6. Write tests

```python
# transforms/my_transform.py
from infrahub_sdk.transforms import InfrahubTransform

class MyTransform(InfrahubTransform):
    query = "my_config_query"

    async def transform(self, data: Any) -> Any:
        return self.render_template(template="my_template.j2", data=data)
```

### Creating a Check

1. Create Python file in `checks/`
2. Inherit from `InfrahubCheck`
3. Implement `validate()` method
4. Add to `.infrahub.yml`
5. Write tests

```python
# checks/my_check.py
from infrahub_sdk.checks import InfrahubCheck

class MyCheck(InfrahubCheck):
    def validate(self, data: Any) -> None:
        if not self.is_valid(data):
            self.log_error("Validation failed", data)
```

### Adding Test Coverage

1. Create test file in `tests/unit/` or `tests/integration/`
2. Use mocks for external dependencies
3. Test both success and failure cases
4. Validate expected behavior

```python
# tests/unit/test_my_component.py
from unittest.mock import Mock, patch
import pytest

class TestMyComponent:
    def test_functionality(self) -> None:
        # Test implementation
        assert True
```

## Jinja2 Templates

### Template Structure
```jinja2
{# templates/configs/my_device/config.j2 #}
! {{ data.name }} Configuration
! Generated by InfraHub Demo

hostname {{ data.name }}

{% for interface in data.interfaces %}
interface {{ interface.name }}
  description {{ interface.description }}
  ip address {{ interface.ip_address }}
{% endfor %}
```

### Template Best Practices
- Use descriptive variable names
- Add comments for clarity
- Handle missing data gracefully
- Follow device-specific syntax

## GraphQL Queries

### Query Structure
```graphql
# queries/config/my_query.gql
query GetMyConfiguration($device_name: String!) {
  DcimGenericDevice(name__value: $device_name) {
    edges {
      node {
        id
        name { value }
        interfaces {
          edges {
            node {
              name { value }
              description { value }
            }
          }
        }
      }
    }
  }
}
```

## Development Workflow

### Setup Development Environment
```bash
# Clone and setup
git clone <repository>
cd infrahub-demo
uv sync

# Start InfraHub
uv run invoke start

# Load schemas and data
uv run infrahubctl schema load schemas
uv run infrahubctl object load data/bootstrap
```

### Making Changes
1. Create feature branch
2. Implement changes following patterns
3. Add/update tests
4. Run quality checks
5. Test functionality
6. Submit pull request

### Quality Checklist
- [ ] Type hints added to all functions
- [ ] Tests written and passing
- [ ] Code formatted with ruff
- [ ] mypy type checking passes
- [ ] Documentation updated
- [ ] InfraHub configuration updated

## InfraHub-Specific Patterns

### Data Loading
```bash
# Load schemas
uv run infrahubctl schema load schemas --branch main

# Load objects
uv run infrahubctl object load data/bootstrap --branch main

# Load menu
uv run infrahubctl menu load menu --branch main
```

### Branch Management
```bash
# Create branch
uv run infrahubctl branch create feature-branch

# Load data to branch
uv run infrahubctl object load data/ --branch feature-branch
```

### Running Generators
```bash
# Via CLI
uv run infrahubctl run generators/my_generator.py

# Via InfraHub UI
# Navigate to Actions -> Generator Definitions -> Run
```

## Error Handling

### Common Issues
1. **Schema conflicts**: Check inheritance and naming
2. **Type mismatches**: Ensure proper type hints
3. **Missing dependencies**: Run `uv sync`
4. **Test failures**: Check mocks and assertions

### Debugging Tips
- Use logging for troubleshooting
- Check InfraHub logs for errors
- Validate GraphQL queries independently
- Test templates with sample data

## Resources

- [InfraHub Documentation](https://docs.infrahub.app)
- [Project README](../README.md)
- [InfraHub SDK Documentation](https://docs.infrahub.app/python-sdk/)
- [Project Discussions](https://github.com/t0m3kz/infrahub-demo/discussions/)

## Best Practices Summary

1. **Always use `uv` for dependency management**
2. **Write tests for every new functionality**
3. **Follow InfraHub naming conventions**
4. **Use type hints consistently**
5. **Document code thoroughly**
6. **Follow schema inheritance patterns**
7. **Mock external dependencies in tests**
8. **Run quality checks before committing**
9. **Test with real InfraHub instance when possible**
10. **Keep queries focused and efficient**