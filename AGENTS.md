# Universal guidance for AI coding assistants working in this repository

## Guiding Principles

- **Schema-Driven**: All automation, configurations, and operations are derived from the data models defined in the schemas. The schema is the single source of truth.
- **Idempotent Operations**: Every script, generator, and transform must be idempotent. Running an operation multiple times should not result in errors or unintended side effects.
- **Test-Driven Development**: Every new feature, bug fix, or change must be accompanied by comprehensive tests. No code should be committed without passing tests.
- **Immutability**: Use an immutable approach by creating new branches for changes. Avoid making direct changes to the `main` branch.

## Project Overview

This is the **infrahub-demo** project - a comprehensive demonstration of design-driven network automation using [InfraHub](https://docs.infrahub.app). The project showcases how to build scalable network infrastructure through schemas, generators, transforms, and validation checks.

Key use cases demonstrated:

- Composable data center, POP and Cloud topology generation
- Configuration management with Jinja2 templates
- Validation checks for network devices
- Infrastructure-as-code patterns

## Package Manager & Dependencies

- **Package Manager**: Use `uv` for all dependency management.
- **Python Version**: Supports Python 3.10, 3.11, or 3.12.
- **Key Dependencies**: `infrahub-sdk[all]>=1.7.2,<2.0.0`, `invoke>=2.2.0`.

### Common `uv` Commands

```bash
# Setup project and install dependencies
uv sync

# Install development dependencies
uv sync --group dev

# Run commands within the virtual environment
uv run pytest
uv run infrahubctl schema load schemas
uv run invoke start
```

## Project Architecture

### Directory Structure

```bash
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

1. **Schemas** - Define data models and relationships.
2. **Generators** - Create topology and infrastructure.
3. **Transforms** - Process data for device configurations.
4. **Checks** - Validate configurations and connectivity.
5. **Templates** - Generate device-specific configurations.

### Data Flow

```text
Schema Definition → Data Loading → Generator Execution → Transform Processing → Configuration Generation
                                         ↓
                                   Validation Checks
```

### Key Files

- `.infrahub.yml` - Central registry for all components (transforms, generators, checks, queries)
- `tasks.py` - Invoke task definitions for automation
- `pyproject.toml` - Project dependencies and tool configuration

## Development Workflow

When implementing a new feature or making a change, follow this thought process:

1. **Understand the Goal**: Deconstruct the user's request into smaller, actionable steps.
2. **Identify Schema Changes**: Determine if any data models in `schemas/` need to be created or updated first. The schema is the foundation.
3. **Locate Core Logic**: Find the relevant generators, transforms, or checks that need to be modified.
4. **Implement the Change**: Write the code, following the established patterns in this document.
5. **Write/Update Tests**: Create new tests in `tests/` or update existing ones to cover all changes.
6. **Run Validation**: Execute `uv run invoke validate` to ensure all quality checks pass before committing.
7. **Do not auto-commit**: only commit when explicitly requested

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

- **Base schemas** (`schemas/base/`): Core models (DCIM, IPAM, Location, Topology, Platform generics).
- **Extension schemas** (`schemas/extensions/`): Feature-specific extensions and implementations.

#### Schema Architecture & Namespace Strategy

This project follows a **layered architecture** with clear separation between platform abstractions and technology implementations:

```text
┌─────────────────────────────────────────────────────────────┐
│                    PLATFORM LAYER                           │
│  Technology-agnostic base abstractions (schemas/base/)      │
├─────────────────────────────────────────────────────────────┤
│  Platform.NetworkSegment    # Base for all segments        │
│  Platform.LoadBalancer      # Base for all LBs             │
├─────────────────────────────────────────────────────────────┤
│              CUSTOMER ABSTRACTIONS                          │
│  Customer-facing logical views (schemas/extensions/)        │
├─────────────────────────────────────────────────────────────┤
│  Customer.*                 # Customer logical abstractions │
│    Customer.VirtualCloud    # Multi-region cloud deployment│
│    Customer.VirtualFabric   # Virtual data center slice    │
├─────────────────────────────────────────────────────────────┤
│              IMPLEMENTATION LAYERS                          │
│  Technology-specific implementations (schemas/extensions/)  │
├─────────────────────────────────────────────────────────────┤
│  OnPrem.*                   # On-premises infrastructure    │
│    OnPrem.NetworkSegment    # VLANs, VXLANs, VRFs         │
│    OnPrem.LoadBalancer      # VIP/HAProxy/F5/Nginx        │
│                                                             │
│  Cloud.*                    # Public cloud resources        │
│    Cloud.Subnet             # Cloud network segments        │
│    Cloud.LoadBalancer       # ALB/NLB/Azure LB             │
│                                                             │
│  Hybrid.*                   # Cross-platform (future)       │
│  Edge.*                     # Edge computing (future)       │
├─────────────────────────────────────────────────────────────┤
│              FUNCTIONAL DOMAINS                             │
│  Routing.*, Security.*, Loadbalancer.*, Service.*          │
└─────────────────────────────────────────────────────────────┘
```

**Namespace Guidelines**:

- **Platform**: Technology-agnostic base generics that provide common classification (segment_type, lb_type)
- **Customer**: Customer-facing logical abstractions that span multiple technologies (VirtualCloud, VirtualFabric)
- **OnPrem**: On-premises/self-hosted infrastructure (physical data centers, private cloud)
- **Cloud**: Public cloud providers (AWS, Azure, GCP, Oracle Cloud)
- **Dcim**: Physical infrastructure and devices
- **Ipam**: IP address management
- **Location**: Geographic hierarchy
- **Topology**: Deployment structures and hierarchies
- **Routing**: Routing protocols and configurations
- **Security**: Security policies, zones, and objects
- **Service**: Running service instances (OSPF, BGP, PIM services)
- **Loadbalancer**: Load balancing support objects (Server, HealthCheck)

**Naming Convention Best Practice**:

- `Service.*` → `OnPrem.*` for clarity (recommended for future refactoring)
- Alternatives considered: `Private.*`, `SelfHosted.*`, `Enterprise.*`
- **Recommended**: `OnPrem` (clearest distinction from Cloud)

#### Schema Best Practices

**1. Generic Design Pattern**:

```yaml
# Base generic (schemas/base/)
generics:
  - name: NetworkSegment
    namespace: Platform
    description: "Base for all network segmentation"
    attributes:
      - name: segment_type  # Classification only
        kind: Dropdown
        choices:
          - vlan
          - vxlan
          - vpc
          - subnet
      - name: status        # Common lifecycle
        kind: Dropdown
    relationships:
      - name: ip_prefixes   # Common relationships
        peer: IpamPrefix
        cardinality: many

# On-prem implementation (schemas/extensions/)
nodes:
  - name: NetworkSegment
    namespace: OnPrem  # or Service (current)
    inherit_from:
      - PlatformNetworkSegment  # Gets segment_type, status, ip_prefixes
      - OnPremGeneric           # Gets deployment, owner metadata
    attributes:
      - name: vlan_id         # Technology-specific
        kind: Number
      - name: vni             # Technology-specific
        kind: Number

# Cloud implementation
nodes:
  - name: Subnet
    namespace: Cloud
    inherit_from:
      - PlatformNetworkSegment  # Gets segment_type, status, ip_prefixes
      - CloudResource            # Gets cloud_id, account metadata
    attributes:
      - name: is_public       # Cloud-specific
        kind: Boolean
    relationships:
      - name: availability_zone  # Cloud-specific
        peer: TopologyCloudAZ
```

**2. Avoid Attribute Conflicts**:

- Base generics should NOT define attributes that technology-specific parents already provide
- Example: Don't define `name`, `description`, `status` in Platform generics if CloudResource or ServiceGeneric already provide them
- Only define attributes truly unique to the concept (e.g., `segment_type` for NetworkSegment, `lb_type` for LoadBalancer)

**3. Multiple Inheritance Order**:

```yaml
inherit_from:
  - PlatformGeneric      # Provides classification (segment_type, lb_type)
  - TechnologyGeneric    # Provides metadata (name, description, cloud_id, deployment)
  - FeatureGeneric       # Provides domain features (interfaces, services)
```

**4. Uniqueness Constraints**:

```yaml
uniqueness_constraints:
  - [account, name__value]           # For cloud resources
  - [deployment, owner, name__value] # For on-prem resources
  - [vpc, cidr]                      # For technical uniqueness
```

Note: Use `__value` suffix for attributes, `__id` suffix for relationships

**5. Relationship Identifiers**:

When a node has multiple relationships to the same peer, use unique identifiers:

```yaml
relationships:
  - name: requester_vpc
    peer: CloudVirtualNetwork
    identifier: "cloud_peering_requester"  # Unique identifier
  - name: accepter_vpc
    peer: CloudVirtualNetwork
    identifier: "cloud_peering_accepter"   # Different identifier
```

**6. Order Weights**:

- Organize attributes logically in the UI
- Use increments of 50-100 to allow insertions
- Standard ranges:
  - 1-200: Core identification (name, description)
  - 200-500: Classification and status
  - 500-1000: Technical attributes
  - 1000+: Relationships

**7. Selective Relationship Inheritance**:

Use intermediate generics to add relationships only to specific node types:

```yaml
# Problem: Want cables on physical deployments (DC, Colocation) but NOT on cloud
# Solution: Create an intermediate generic

# Base deployment (all types inherit)
generics:
  - name: Deployment
    namespace: Topology
    # Common deployment attributes

  # Physical deployment only
  - name: PhysicalDeployment
    namespace: Topology
    relationships:
      - name: cables
        peer: DcimCable
        identifier: "physical_deployment__cables"

# Implementations
nodes:
  - name: DataCenter
    inherit_from:
      - TopologyDeployment
      - TopologyPhysicalDeployment  # Gets cables relationship
  
  - name: ColocationAZ
    inherit_from:
      - TopologyDeployment
      - TopologyPhysicalDeployment  # Gets cables relationship
  
  - name: CloudRegion
    inherit_from:
      - TopologyDeployment           # NO cables relationship

# Cable extension (single field, not multiple)
extensions:
  nodes:
    - kind: DcimCable
      relationships:
        - name: deployment
          peer: TopologyPhysicalDeployment  # Points to intermediate generic
          identifier: "physical_deployment__cables"
```

**Benefits**:
- Single `deployment` field on DcimCable (not `dc_deployment`, `colocation_deployment`, etc.)
- Cables only appear on physical infrastructure
- Cloud deployments remain clean
- Easy to add new physical deployment types


#### Schema Structure

```yaml
nodes:
  - name: MyNode
    namespace: MyNamespace
    description: "Description of the node"
    label: "Display Label"
    icon: "mdi:icon-name"
    include_in_menu: false
    inherit_from:
      - PlatformGeneric
      - TechnologyGeneric
    attributes:
      - name: my_attribute
        kind: Text
        optional: false
        order_weight: 1000
        description: "Attribute description"
    relationships:
      - name: my_relation
        peer: RelatedNode
        cardinality: many
        optional: true
        order_weight: 2000
        identifier: "unique_rel_id"  # If multiple relations to same peer
    uniqueness_constraints:
      - [peer_relationship, name__value]
```

#### Attribute Kinds

Valid attribute kinds (case-sensitive):

- `Text` - String values
- `Number` - Numeric values (integer or float)
- `Boolean` - True/False
- `Dropdown` - Single selection from choices
- `List` - Multiple values (NOT `Tags`)

**Never use**: `String`, `Tags`, `Array`

### Naming Conventions

#### File Naming

- Schemas: `snake_case.yml`
- Python modules: `snake_case.py`
- Classes: `PascalCase`
- Functions/methods: `snake_case`

#### InfraHub Naming

- **Nodes**: `PascalCase` (e.g., `LocationBuilding`, `TopologyPod`, `NetworkSegment`)
- **Attributes**: `snake_case` (e.g., `device_type`, `serial_number`, `segment_type`)
- **Relationships**: `snake_case` (e.g., `parent_location`, `connected_to`, `ip_prefixes`)
- **Namespaces**: `PascalCase` (e.g., `Platform`, `OnPrem`, `Cloud`, `Dcim`, `Ipam`, `Routing`)

**Namespace Naming Conventions**:

- Use singular form for technology domains: `Onprem`, `Cloud`, `Edge` (not `Onprems`, `Clouds`)
- Functional domains: `Routing`, `Security`, `Loadbalancer`, `Service`
- Avoid abbreviations unless industry-standard: `Dcim` (ok), `Ipam` (ok), `LB` (avoid - use `Loadbalancer`)

**Node Naming Conventions**:

- OnPrem resources: Use generic business names (`NetworkSegment`, `LoadBalancer`, `Firewall`)
- Cloud resources: Use provider-agnostic names (`Subnet`, `LoadBalancer`, `VPC`)
- Customer abstractions: Use logical names (`VirtualCloud`, `VirtualFabric`)
- Avoid technology-specific prefixes in node names (`NetworkSegment` not `VLANSegment`)

**Implementation Status**:

- ✅ `Platform.*` - Base generics for classification
- ✅ `Customer.*` - Customer-facing logical abstractions (VirtualCloud, VirtualFabric)
- ✅ `OnPrem.*` - On-premises infrastructure (NetworkSegment, LoadBalancer)
- ✅ `Cloud.*` - Public cloud resources (Subnet, LoadBalancer, VPC)
- ✅ `Service.*` - Running service instances (OSPF, BGP, PIM)

## Testing Requirements

### Testing Strategy

- **Unit Tests**: Mock-based testing with `unittest.mock`. These should be fast, isolated, and mock all external dependencies.
- **Integration Tests**: Full workflow validation against a running Infrahub instance.
- **Every functionality MUST be tested**, covering both success and failure scenarios.

### Test Structure

The test suite uses `pytest` and is organized into `unit`, `integration`, and `smoke` tests.

```text
tests/
├── conftest.py       # Root pytest fixtures (session-scoped)
├── unit/             # Fast, isolated unit tests
│   ├── test_*.py     # Unit test files
│   └── simulators/   # Mock data and simulators
├── integration/      # Tests requiring running Infrahub
│   ├── conftest.py   # Integration-specific fixtures
│   ├── data/         # Test data files
│   └── test_*.py     # Integration test files
└── smoke/            # Quick smoke tests
```

### Writing Tests

- **Type hints and docstrings are required** for all test functions.
- Use descriptive names for tests (e.g., `test_create_device_with_invalid_name_raises_error`).
- Use `pytest.mark.asyncio` for async tests and `pytest.mark.parametrize` for testing multiple inputs.

### Running Tests

```bash
# Run all tests
uv run pytest

# Run specific test categories
uv run pytest tests/unit/
uv run pytest tests/integration/

# Run with coverage
uv run pytest --cov=.
```

### Fixtures and Mocking

- Use fixtures for shared resources (`root_dir`, `infrahub_client`, etc.).
- Mock the Infrahub SDK and GraphQL responses in unit tests. Store mock data in `tests/unit/simulators/`.
- Avoid hardcoded paths; use fixtures like `root_dir`.

## Security Considerations

- Never commit `.env` files or credentials
- API tokens in documentation are demo tokens for local development only
- Avoid introducing OWASP top 10 vulnerabilities (XSS, SQL injection, command injection)
- Validate external inputs at system boundaries

## Code Quality Standards

- **Formatting**: Run `uv run ruff check . --fix` before committing.
- **Type Checking**: Run `uv run ty check .` to ensure type safety.
- **Validation**: Run `uv run invoke validate` to execute all quality checks.

## Common Pitfalls

- **Forgetting to load schema changes**: After modifying a schema, always run `uv run infrahubctl schema load schemas` on the correct branch.
- **Hardcoded paths in tests**: Always use fixtures like `root_dir` to build paths dynamically.
- **Non-idempotent generators**: Ensure generators can be run multiple times without creating duplicate objects or causing errors.
- **Ignoring `order_weight`**: Forgetting to set `order_weight` in schemas can lead to an inconsistent UI.
- **Attribute conflicts in generics**: Base generics (Platform.*) should NOT redefine attributes from technology generics (CloudResource, ServiceGeneric)
- **Missing relationship identifiers**: When multiple relationships point to the same peer, always specify unique `identifier` values

## Architecture Decision Records

### Namespace Strategy (2026-01)

**Decision**: Use technology-based namespaces (`OnPrem`, `Cloud`) rather than functional (`Service`, `Connectivity`)

**Rationale**:

1. **Operational alignment** - Teams typically manage "cloud resources" or "on-prem infrastructure" separately
2. **Clear boundaries** - Easy to apply different policies, generators, or transforms per technology
3. **Query simplicity** - `kind=Cloud*` gets all cloud resources, `kind=OnPrem*` gets all on-premises
4. **Future extensibility** - Easy to add `Container.*`, `Edge.*`, `IoT.*` namespaces
5. **RBAC granularity** - Can assign permissions by technology domain

**Current State** (transition in progress):

- `Service.*` → Rename to `OnPrem.*` (recommended)
- `Cloud.*` → Keep as-is ✅
- `Platform.*` → Base generics ✅

**Alternatives considered**:

- `Private.*` - Too vague, could mean security level
- `SelfHosted.*` - Too long, less common terminology
- `Enterprise.*` - Doesn't distinguish from cloud enterprise features
- `OnPrem.*` - ✅ **Selected**: Clear, industry-standard, distinguishable from Cloud

### Generic Pattern (2026-01)

**Decision**: Platform generics provide ONLY classification attributes specific to the concept

**Rationale**:

1. Avoid attribute conflicts when inheriting from multiple generics
2. Technology generics (CloudResource, ServiceGeneric) already provide name, description, status
3. Platform generics should add value through classification (segment_type, lb_type) not duplication

**Pattern**:

```yaml
# ✅ Good: Platform generic adds classification
Platform.NetworkSegment:
  - segment_type (vlan/vxlan/vpc/subnet)
  - ip_prefixes (relationship)

# ✅ Good: Platform generic adds classification
Platform.LoadBalancer:
  - lb_type (application/network/hardware/software)

# ❌ Bad: Platform generic duplicates common attributes
Platform.NetworkSegment:
  - name, description, status  # Already in CloudResource/ServiceGeneric
  - segment_type
```

**Implementation Guide**:

- Platform generics: Classification attributes only
- Technology generics (CloudResource, OnPremGeneric): Metadata (name, description, cloud_id, deployment)
- Feature generics (ServiceGenericInterfaces): Domain-specific features

## Resources

- [InfraHub Documentation](https://docs.infrahub.app)
- [Project README](../README.md)
- [InfraHub SDK Documentation](https://docs.infrahub.app/python-sdk/)
