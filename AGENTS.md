# Universal guidance for AI coding assistants working in this repository

## Guiding Principles

- **Schema-Driven**: All automation, configurations, and operations are derived from the data models defined in
  the schemas. The schema is the single source of truth.
- **Idempotent Operations**: Every script, generator, and transform must be idempotent. Running an operation
  multiple times should not result in errors or unintended side effects.
- **Test-Driven Development**: Every new feature, bug fix, or change must be accompanied by comprehensive tests.
  No code should be committed without passing tests.
- **Immutability**: Use an immutable approach by creating new branches for changes. Avoid making direct changes
  to the `main` branch.

## Project Overview

This is the **infrahub-demo** project - a comprehensive demonstration of design-driven network automation using
[InfraHub](https://docs.infrahub.app). The project showcases how to build scalable network infrastructure through
schemas, generators, transforms, and validation checks.

Key use cases demonstrated:

- Composable data center, POP and Cloud topology generation
- Configuration management with Jinja2 templates
- Validation checks for network devices
- Infrastructure-as-code patterns

## Package Manager & Dependencies

- **Package Manager**: Use `uv` for all dependency management.
- **Python Version**: Supports Python 3.11 to 3.14.
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
2. **Identify Schema Changes**: Determine if any data models in `schemas/` need to be created or updated first.
   The schema is the foundation.
3. **Locate Core Logic**: Find the relevant generators, transforms, or checks that need to be modified.
4. **Implement the Change**: Write the code, following the established patterns in this document.
5. **Write/Update Tests**: Create new tests in `tests/` or update existing ones to cover all changes.
6. **Run Validation**: Execute `uv run invoke validate` to ensure all quality checks pass before committing.
7. **Do not auto-commit**: only commit when explicitly requested
8. **Follow Conventional Commits**: when a commit is requested, use the conventional commits format (see below)

### Simplicity Rules

Duplication here usually starts as "copy the sibling generator/template and edit it", then the copies drift
(one keeps a lock or race guard, the other loses it). To prevent that:

- **One implementation per flow**: grep for the flow (e.g. `allocate_next_ip_prefix`, `TemplateDcimVirtualDevice`,
  a template stanza) before writing it. If it exists, call it, or move it into a mixin/helper and call it from both
  places. Per-facility differences become class attributes (see `generators/firewall_context.py`).
- **Fix shared code, don't special-case call sites**.
- **No dead knobs**: no flag, parameter, branch or wrapper that nothing in production reaches. Code covered only by its
  own tests is dead.
- **No redundant round trips**: one `filters(ids=[...])` instead of `get()` in a loop; no `fetch()` after `include=`;
  filter by `name__value` instead of listing and scanning.
- **Template refactors keep output byte-identical**; render the golden fixtures before and after and diff.

For a cleanup sweep, use the `infrahub-simplifying-code` skill (`.claude/skills/`). For change reviews, the review
board includes a `simplification-reviewer`.

## Commit Message Standard

This project uses **Conventional Commits** enforced by `commitizen`.

### Format

```text
<type>(<optional scope>): <short summary>

[optional body — explain the why, not the what]

[optional footer: BREAKING CHANGE: ...]
```

### Types

| Type | When to use | Version bump |
| --- | --- | --- |
| `feat` | New feature or capability | minor (0.x.0) |
| `fix` | Bug fix | patch (0.0.x) |
| `chore` | Maintenance, deps, tooling — no functional change | patch |
| `docs` | Documentation only | patch |
| `refactor` | Code restructure without behaviour change | patch |
| `test` | Adding or fixing tests | patch |
| `perf` | Performance improvement | patch |
| `ci` | CI/CD pipeline changes | patch |
| `feat!` or `BREAKING CHANGE:` footer | Breaking API/schema change | major (x.0.0) |

### Examples

```text
feat: add universal topology demo with end-to-end graph tracing

fix: strip trailing newline from sonic_mclag.j2 when mlag is None

chore: remove dead CustomerVirtualCloud schema

docs: reframe universal topology README around graph tracing purpose

refactor: distribute geography/IPs into respective subfolders

feat(schema)!: rename ServiceGeneric to ManagedGeneric

BREAKING CHANGE: all ServiceGeneric references must be updated to ManagedGeneric
```

### Scope (optional)

Use scope to narrow the area: `schema`, `generator`, `template`, `check`, `transform`,
`data`, `ci`, `deps`.

## Release Workflow

Most PRs (bug fixes, features, dependabot) are merged without any version bump.
When you are ready to ship a release, create a dedicated release PR:

```bash
uv run invoke release        # bumps pyproject.toml, updates CHANGELOG.md, tags
git push && git push --tags  # push branch + tag together
```

Open the PR and merge. CI detects the `v*` tag and creates the GitHub Release
automatically with generated release notes.

See [CONTRIBUTING.md](CONTRIBUTING.md) for full details.

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

- **Base schemas** (`schemas/base/`): DCIM, IPAM, Location, Organization, App instance and the `ManagedNetworkSegment`
  generic.
- **Extension schemas** (`schemas/extensions/<domain>/`): every other node, generic and extension.

#### Namespaces

| Namespace | Holds |
| --------- | ----- |
| Managed | On-prem services and the shared generics `ManagedGeneric` (name, description, status), `ManagedGenericDevice` (the device `capabilities` edge), `ManagedGenericInterfaces` (the interface-side `capabilities` edge; its identifier stays `interface_capabilities`), `ManagedNetworkSegment` and `ManagedLoadBalancer` |
| Cloud | Provider-hosted resources (public cloud and provider-hosted network services); every resource inherits `CloudResource`, except `CloudNetworkAppliance`, a provider-hosted network function (Equinix Network Edge) that inherits the device generics (`DcimDevice`, `DcimCapabilities`) so its interfaces, BGP and HA stay device-shaped |
| Topology | Deployments (`TopologyDataCenter`, `TopologyPod`, `TopologyColocationMetro`, ...), design elements, circuits (`TopologyPhysicalCircuit` and the virtual-circuit kinds `TopologyTunnelCircuit`, `TopologyInterconnectCircuit`, `TopologyHandoffCircuit`) and the tenant footprints `TopologyCustomer*` |
| Dcim, Ipam, Location, Organization | Devices/interfaces/cables, prefixes/addresses/namespaces, geography, providers/customers |
| Routing | Shared routing objects (`RoutingAutonomousSystem`, `RoutingOSPFArea`, `RoutingPassword`) |
| Security, Loadbalancer, Proxy, App, Virt | Functional domains |

- **One Managed generic per concept, with an implementation per technology**: `ManagedVlanSegment`,
  `ManagedVxlanSegment` and `CloudNetworkSegment` inherit `ManagedNetworkSegment`; `ManagedLoadbalancerHA` and
  `CloudLoadBalancer` inherit `ManagedLoadBalancer`.
- **Topology.Customer**: Per-facility tenant footprints (`TopologyCustomerDC`, `TopologyCustomerColocation`,
  `TopologyCustomerCloud`) — not a separate `Customer.*` namespace; these inherit `TopologyConnectableLocation`
  alongside the facility nodes so a circuit endpoint is one shared relationship, not a duplicated model. The
  `TopologyCustomer` generic carries only the owner-derived `name`, `infrastructure_orchestrator` and `owner`.
  `TopologyEnvironmentScoped` adds the required `environment` and only DC and Colocation footprints inherit it;
  a cloud footprint has no environment (`CloudAccount.environment` and the segments carry it).
- **Office**: `TopologyCustomerOffice` is not a `TopologyCustomer`. The owner sits directly on it, its name is
  `{OWNER}-OFFICE` (for example `C005-OFFICE`), it is always PROD (no `environment`) and its parent is the
  "Campus and Office" `TopologyLabel`.
- **Circuit placement**: a physical circuit is a pure carrier (fibre, cross-connect, internet access) with no role
  and no session; its `locations` peer `TopologyFacility`: the DC (never a pod, rack or footprint), the colocation
  cage `TopologyColocationZone` or the provider-hosted `TopologyVirtualColocation` (never the metro), the
  `TopologyCloudRegion` (never a zone or `TopologyCloud`), the ISP `TopologyExternalProviders`, the
  `TopologySaasRegion` or the `TopologyCustomerOffice`. Data lists only the cage; `add_circuit` adds every end
  whose endpoint device's `deployment` is a facility (border leaf -> DC, provider router -> region, ISP PE -> ISP),
  when the circuit is created or its ends change: nothing re-runs it on a later `deployment` edit (the check reports
  it). A physical circuit is shared underlay, unowned, except an office's access circuit (`add_sdwan_edge`'s
  `INET-<office>`), owned by the office's owner. `locations` is declared on each circuit node, not on
  `TopologyCircuit`: an inherited peer cannot be narrowed. `checks/circuit_placement.py` guards what the schema
  cannot, per kind:
  - **Physical**: exactly two distinct locations, one per end device's `deployment`; owned exactly when it ends on a
    `TopologyCustomerOffice`.
  - **Tunnel**: two locations and two ports, owned by the office's owner, every end port rides a live internet
    physical circuit of its own (no contiguity rule).
  - **Interconnect**: unowned, two locations and two ports, both ports carried, the carriers found at the two ports
    form one contiguous path between the two locations and a single carrier is pure fibre (its two ends are the
    circuit's two ports); each end device has exactly one `overlay` `ManagedBGP`.
  - **Handoff**: one or two locations (both ends may sit in one cage); at least one end on a physical device is
    carried (a live physical circuit or a `DcimCable` on the port or its `parent_interface`); both ports are
    sub-interfaces `<port>.<vlan>` with the same VLAN (the cloud endpoint's `vlan_id` too).
  - **Session circuits (Interconnect, Handoff)**: a port is an end of at most one of them. **Environment class**:
    circuits meeting at a device share a class (prod vs non-prod; a handoff takes its `ip_namespace`'s
    `namespace_type`, a tunnel is PROD, an interconnect has none).
- **Virtual circuits**: `TopologyVirtualCircuit` (generic) is the connection between topologies on which routing is
  generated or recorded; its role is its KIND, never an attribute. `TopologyTunnelCircuit` (SD-WAN office edge <->
  gateway `eth1`, and the partner tunnel; inventory only, no session, owner = the office),
  `TopologyInterconnectCircuit` (DCI, unowned: a `/31` or `/127` eBGP underlay plus a loopback-to-loopback EVPN
  multihop session keyed per circuit, MTU 9216) and `TopologyHandoffCircuit` (our edge sub-interface <-> a party
  device sub-interface in one environment namespace: cloud on-ramps, Equinix/Megaport, the SD-WAN gateway LAN; a `/31`
  from `Handoff-<namespace>-IPv4` and one eBGP session; the cloud or provider side address comes from OUR pool only,
  and for AWS Direct Connect the customer specifies the peer IPs). One generator, `add_virtual_circuit`
  (`VirtualCircuitSessionGenerator`), builds the sessions of Interconnect and Handoff circuits; `add_circuit` only
  derives physical locations and fans out to it. The carrier of a virtual circuit is derived, never listed: the
  physical circuits (or a `DcimCable` inside one facility) on its port or its `parent_interface`
  (`utils/circuit_underlay.py`). The only role that is an output is `ManagedBGPPeering.peering_role` (`regular`,
  `dci`, `dci-underlay`, `fabric`, `overlay`, `underlay`). The tenant impact of a fibre is a graph traversal (circuit ->
  ports -> segments, peerings, circuits; filter `capabilities` by kind), not a per-tenant circuit.
- **Border policy**: a `regular` session in a PROD or NON-PROD VRF is external unless its far end is ours: the far
  end's BGP process in that peering has `process_role: overlay` (not when the far end device is a
  `CloudNetworkAppliance`, a provider-hosted Network Edge: that hop is external), or the far end is the office's own
  SD-WAN edge (`deployment.lisp_wan_edges`). The device owner is never the signal. A handoff circuit is unowned; the owner of a
  prefix is the object that registers it (`CloudVirtualNetwork.account.owner` for a VPC block, the office's owner for
  `lan_prefixes`, the tunnel's owner for a `TopologyTunnelCircuit.registered_prefixes` range such as a B2B partner's,
  `segment.owner` for a segment), and only an `OrganizationCustomer` counts. An external session
  accepts its owners' registered prefixes (`RM-<session>-IN`) and is announced (`RM-<session>-OUT`) what those same
  owners have on-prem or registered behind another external session of the namespace (an office LAN reaches the
  owner's VPC, its VPC block reaches the office), minus its own IN: the edge passes routes between two external
  sessions of the SAME owner, never across owners, and never back to the session a prefix was learned on (an
  office-hosted segment is registered behind its gateway, so it is not local space). Exact match, retired segments
  dropped, one helper `utils/ownership.py` `exchange_policies`. A prefix of two owners, or an owner's prefix behind
  two sessions, is a WARNING in the transform and an error of `checks/routing_invariants.py` rule 8. With no owner it
  accepts and announces nothing. Every destination prefix of an enabled `SecurityPolicyRule` must be on-prem space or
  registered behind an external session (VPC block, office LAN or hosted segment, tunnel range), in a namespace of the
  source segment's environment: otherwise `checks/segment_security.py` reports "not reachable: no session accepts it"
  (a `destination_segment` rule is not checked; the internet is a segment, not a prefix).
  Only the NX-OS edge renders these maps; anywhere else a transform refuses an external session
  (`BaseDeviceTransform.external_policy_platforms`), except a hosted `CloudNetworkAppliance` on cisco_ios/cisco_xe
  (`UNFILTERED_BANNER_PLATFORMS`): it renders an `UNFILTERED` comment above the session and logs a WARNING, because a
  provider-hosted device must not fail its artifact. A handoff peering sends no communities.
- **DC service tier**: `TopologyCustomerTemplateDC` (referenced by the footprint's `design`, for example `M_DC`)
  holds `dedicated_firewall`, `dedicated_loadbalancer`, `max_segments`, `ports_25g` and `ports_100g` (0 = no
  host-facing ports of that speed). `checks/customer_quota.py` enforces the segment and port numbers; a footprint
  with no `design` is not metered. `add_customer_deployment_dc` (triggered on creation only) links the footprint's
  `serving_loadbalancer_context`; when a re-run moves it to another context (dedicated <-> shared), it re-runs
  `add_app_component` for the footprint's load-balanced components, so their VIPs, R_server and callers' R_client
  rules follow. A `design` change alone re-runs nothing, and a hand edit of the link is not followed up.
- **Fabric design**: `TopologyDataCenter.design` (`TopologyTemplateDC`: S, M, L, XL) and `TopologyPod.design`
  (`TopologyTemplatePod`: S_MIDDLE, S_TOR, ...) are required and hold only the envelope (`max_*`); the rows live in
  `data/bootstrap/14b_fabric_templates.yml`. Every fabric pool size is derived from them by
  `generators/fabric_sizing.py`, and a pod's design must be one of its DC design's `pod_templates`.
- **Suite capacity**: a `LocationSuite` is the physical capacity: `rows` and `racks_per_row` (both required) are its
  grid. `checks/suite_capacity.py` enforces that every rack sits inside it (`row_index <= rows`,
  `index <= racks_per_row`, where `index` is the slot in the row) and that each hosting suite fits the pod's design
  envelope (`max_rows`, `max_compute_racks_per_row + max_network_racks_per_row`). Racks are data, never generated.
- **Cable attributes**: a generator-created `DcimCable` carries `cable_id` (the short printed label, a hash of the
  endpoint labels), `color` (the TIA-598 jacket colour of its `type`) and `length_cm`, derived from the suite layout
  (`rack_pitch_cm`, `row_pitch_cm`; `generators/helpers/cable_attributes.py`). The length is unset when an end is not
  in a rack or the ends are in different suites. Cables declared in data are not generated and stay unset.
- **Capability edges**: a capability reaches devices via `ManagedGenericDevice` and interfaces via
  `ManagedGenericInterfaces`. Inherit the second only when something reads the interface edge.

#### Schema Best Practices

**1. Generic Design Pattern**:

- A generic carries what every implementation shares. `ManagedNetworkSegment` holds `segment_type`,
  `ManagedVxlanSegment` adds `vni`, and `CloudNetworkSegment` adds the VPC side (`virtual_network`).
- Never redefine on a node an attribute that a generic provides (there is no `allow_override`).
- Inheritance order: `ManagedGeneric` or `CloudResource` first, then the concept generic, then the edge generics.

```yaml
inherit_from:
  - ManagedGeneric           # name, description, status
  - ManagedNetworkSegment    # the concept generic (segment_type, ...)
  - ManagedGenericInterfaces # edge generic: the interface-side capabilities edge (identifier interface_capabilities)
```

**2. Uniqueness Constraints**:

```yaml
uniqueness_constraints:
  - [account, name__value]           # For cloud resources
  - [deployment, owner, name__value] # For on-prem resources
  - [vpc, cidr]                      # For technical uniqueness
```

Note: Use `__value` suffix for attributes, `__id` suffix for relationships

**3. Relationship Identifiers**:

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

**4. Order Weights**:

- Organize attributes logically in the UI
- Use increments of 50-100 to allow insertions
- Standard ranges:
  - 1-200: Core identification (name, description)
  - 200-500: Classification and status
  - 500-1000: Technical attributes
  - 1000+: Relationships

**5. Selective Relationship Inheritance**:

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
      - ManagedGeneric
      - ManagedGenericDevice
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

- **Nodes**: `PascalCase` (e.g., `LocationRack`, `TopologyPod`, `ManagedVxlanSegment`)
- **Attributes**: `snake_case` (e.g., `device_type`, `serial_number`, `segment_type`)
- **Relationships**: `snake_case` (e.g., `parent_location`, `connected_to`, `ip_prefixes`)
- **Namespaces**: `PascalCase` (e.g., `Topology`, `Managed`, `Cloud`, `Dcim`, `Ipam`, `Routing`)

**Namespace Naming Conventions**:

- Use singular form for technology domains: `Managed`, `Cloud` (not `Manageds`, `Clouds`)
- Functional domains: `Routing`, `Security`, `Loadbalancer`, `Proxy`, `App`
- Avoid abbreviations unless industry-standard: `Dcim` (ok), `Ipam` (ok), `LB` (avoid - use `Loadbalancer`)

**Node Naming Conventions**:

- Use generic business names and let the technology be the implementation (`ManagedVxlanSegment`).
- Cloud names are provider-agnostic (`CloudNetworkSegment`, `CloudLoadBalancer`, `CloudVirtualNetwork`).
- Customer footprints are `TopologyCustomer<FacilityType>` (`TopologyCustomerDC`, `TopologyCustomerColocation`).

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
- **No Python module exceeds 1000 lines**: `tests/`, `generators/`, `transforms/`, `checks/` and `utils/` alike
  (guarded by `tests/unit/test_test_file_sizes.py`; the only exemption is the hand-curated `generators/protocols.py`).
  Split by topic. A test module into sibling `test_<name>_<topic>.py` modules; builders used by several of them go
  into one `<name>_helpers.py` (no `test_` prefix, imported as `from tests.unit.<name>_helpers import ...`). A
  generator into a mixin (`generators/<topic>.py` or `generators/helpers/`) that the class inherits; a transform or
  check helper into a sibling module along the topic seam (for example `transforms/helpers/bgp_external.py`).
  Rewrite the importers; do not leave a re-export wrapper behind.

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

### Which integration run a change needs

Integration runs go through `uv run invoke test-integration` (see the `infrahub-running-integration-tests` skill), one
stack at a time. The full 30_all subset is `test_01, 02, 59, 60, 61, 62, 64, 65, 66` (`62` is `test_62`, `62b`, `62c`;
`64` is `test_64`, `64b`); `test_65` (idempotency) is the last ~20 of its ~55 minutes, `test_66` (circuit lifecycle, mutates the branch) runs after it. Match the run to the change:

| Change touches | Run |
| --- | --- |
| Generators (including their helpers), schema, `data/`, or queries used by generators | Full subset **including `test_65` and `test_66`** (a generator's cleanup is only proven by `test_66`) |
| Templates, transforms, checks and their validation queries | Subset `01, 02, 59–64` (no `test_65`, no `test_66`) plus the smoke golden diff |
| Docs, tests only | Unit + smoke |

For ANY schema, query or generator change, run the fast loop BEFORE the full 30_all subset: `uv run invoke
test-integration-fast` on a stack kept alive with `INFRAHUB_TESTING_KEEP_STACK=1`, then `uv run invoke validate-queries`
against that stack. The task runs every `.gql` under `queries/` and `tests/` against the live schema and exits non-zero
on a schema error (`Cannot query field`, `Unknown type`, `Unknown argument`, ...); runtime errors from its dummy
variables are ignored, and exit code 2 means the task itself failed (server unreachable, bad token), not a query. It
takes minutes, where the same error found by the full subset costs tens of minutes. The test stack serves on port 8100
(the default `--address http://localhost:8100`; `--server-port` moves it): pass `--address` for any other server and
`--branch` for a branch other than `main`.

The smoke `syntax_check` entries (`tests/smoke/test_graphql.yml`) are an offline parse with no schema and run in `uv run
invoke validate`; `validate-queries` is the live schema check and needs a stack. Every query under `queries/` needs a
smoke entry (guarded by `tests/unit/test_smoke_query_coverage.py`).

Always, after any run that registers a new check or transform query: run that query against the settled stack
(`KEEP_STACK`) and simulate the check or transform on the real data. Unit tests with canned responses do not catch
schema-invalid queries or false positives on real data. Run `test_65` once on `main` before every release. Clean the
previous stack (`uv run invoke clean-testcontainers`) before starting a run, and confirm a merged PR's `state` is
`MERGED` before relying on it.

### Fixtures and Mocking

- Use fixtures for shared resources (`root_dir`, `infrahub_client`, etc.).
- Mock the Infrahub SDK and GraphQL responses in unit tests. Store mock data in `tests/unit/simulators/`.
- Avoid hardcoded paths; use fixtures like `root_dir`.

## Testing Objects with Python Scripts

### Overview

When validating loaded data or exploring the Infrahub API, build Python scripts on-the-fly using inline Python
with pipe. This approach allows you to create exactly the query you need without maintaining static script files.

### Simple Query Pattern

For basic queries, use inline Python with `-c`:

```bash
# Count objects
uv run python -c "
import asyncio
from infrahub_sdk import InfrahubClient

async def main():
    c = InfrahubClient(address='http://localhost:8000')
    c.default_branch = 'flow'
    r = await c.execute_graphql(query='query { DcimPhysicalDevice { count } }')
    print(f\"Devices: {r['DcimPhysicalDevice']['count']}\")

asyncio.run(main())
"
```

### Multi-Line Query Pattern (Recommended)

For complex queries with grouping and formatting, use echo with pipe:

```bash
echo "import asyncio
from infrahub_sdk import InfrahubClient

async def main():
    c = InfrahubClient(address='http://localhost:8000')
    c.default_branch = 'flow'
    r = await c.execute_graphql(query='''
        query {
            DcimPhysicalDevice {
                edges {
                    node {
                        display_label
                        role { value }
                    }
                }
            }
        }
    ''')

    by_role = {}
    for e in r['DcimPhysicalDevice']['edges']:
        n = e['node']
        role = n['role']['value']
        if role not in by_role:
            by_role[role] = []
        by_role[role].append(n['display_label'])

    print()
    print('='*60)
    print('Devices Grouped by Role')
    print('='*60)
    print()
    for role, devs in by_role.items():
        print(f'📦 {role}: {len(devs)} devices')
        for d in devs:
            print(f'   - {d}')
    print()

asyncio.run(main())" | uv run python
```

**Output**:

```text
============================================================
Devices Grouped by Role
============================================================

📦 edge: 4 devices
   - am5-edge-01
   - am5-edge-02
   - fr5-edge-01
   - fr5-edge-02
📦 border-leaf: 2 devices
   - dc1-border-01
   - dc1-border-02
```

### Common Query Patterns

#### Pattern 1: Count objects by type

```bash
echo "import asyncio
from infrahub_sdk import InfrahubClient

async def main():
    c = InfrahubClient(address='http://localhost:8000')
    c.default_branch = 'flow'

    kinds = ['DcimPhysicalDevice', 'DcimCable', 'TopologyPhysicalCircuit', 'TopologyVirtualCircuit']

    print()
    print('='*60)
    print('Infrastructure Object Counts')
    print('='*60)
    print()

    for kind in kinds:
        r = await c.execute_graphql(query=f'query {{ {kind} {{ count }} }}')
        count = r[kind]['count']
        print(f'{kind:30s}: {count:4d}')
    print()

asyncio.run(main())" | uv run python
```

#### Pattern 2: Group objects by attribute

```bash
echo "import asyncio
from infrahub_sdk import InfrahubClient

async def main():
    c = InfrahubClient(address='http://localhost:8000')
    c.default_branch = 'flow'

    r = await c.execute_graphql(query='''
        query {
            DcimPhysicalDevice {
                edges {
                    node {
                        display_label
                        role { value }
                        status { value }
                    }
                }
            }
        }
    ''')

    by_role = {}
    for e in r['DcimPhysicalDevice']['edges']:
        n = e['node']
        role = n['role']['value']
        if role not in by_role:
            by_role[role] = []
        by_role[role].append(n['display_label'])

    print()
    print('='*60)
    print('Devices Grouped by Role')
    print('='*60)
    print()
    for role, devices in by_role.items():
        print(f'📦 {role}: {len(devices)} devices')
        for device in devices:
            print(f'   - {device}')
    print()

asyncio.run(main())" | uv run python
```

#### Pattern 3: Validate relationships

```bash
uv run python << 'EOF'
import asyncio
from infrahub_sdk import InfrahubClient

async def main():
    client = InfrahubClient(address="http://localhost:8000")
    client.default_branch = "flow"

    result = await client.execute_graphql(query="""
        query {
            TopologyPhysicalCircuit {
                edges {
                    node {
                        display_label
                        circuit_id { value }
                        provider {
                            node {
                                name { value }
                            }
                        }
                        locations {
                            edges {
                                node {
                                    display_label
                                }
                            }
                        }
                    }
                }
            }
        }
    """)

    circuits = result["TopologyPhysicalCircuit"]["edges"]
    missing_provider = []
    missing_location = []

    for edge in circuits:
        circuit = edge["node"]
        circuit_id = circuit["circuit_id"]["value"]

        if not circuit.get("provider") or not circuit["provider"].get("node"):
            missing_provider.append(circuit_id)

        if len(circuit["locations"]["edges"]) < 2:
            missing_location.append(circuit_id)

    print(f"\n{'='*60}")
    print("Circuit Validation Report")
    print(f"{'='*60}\n")
    print(f"Total circuits: {len(circuits)}")
    print(f"Missing provider: {len(missing_provider)}")
    print(f"Missing locations: {len(missing_location)}")

    if missing_provider:
        print("\n⚠️  Circuits without provider:")
        for cid in missing_provider:
            print(f"   - {cid}")

    if missing_location:
        print("\n⚠️  Circuits without locations:")
        for cid in missing_location:
            print(f"   - {cid}")

    if not missing_provider and not missing_location:
        print("\n✅ All circuits have required relationships!")

asyncio.run(main())
EOF
```

#### Pattern 4: Multi-layer connectivity view

```bash
uv run python << 'EOF'
import asyncio
from infrahub_sdk import InfrahubClient

async def main():
    client = InfrahubClient(address="http://localhost:8000")
    client.default_branch = "flow"

    # Physical Layer
    cables_result = await client.execute_graphql(query="""
        query {
            DcimCable {
                edges {
                    node {
                        type { value }
                        endpoints {
                            edges {
                                node {
                                    ... on DcimPhysicalInterface {
                                        name { value }
                                        device {
                                            node {
                                                name { value }
                                            }
                                        }
                                    }
                                }
                            }
                        }
                    }
                }
            }
        }
    """)

    # Circuit Layer
    circuits_result = await client.execute_graphql(query="""
        query {
            TopologyPhysicalCircuit {
                edges {
                    node {
                        circuit_type { value }
                    }
                }
            }
        }
    """)

    # Virtual Layer
    virtual_circuits_result = await client.execute_graphql(query="""
        query {
            TopologyVirtualCircuit {
                edges {
                    node {
                        __typename
                        provider {
                            node {
                                name { value }
                            }
                        }
                    }
                }
            }
        }
    """)

    print(f"\n{'='*80}")
    print("END-TO-END CONNECTIVITY STACK")
    print(f"{'='*80}\n")

    # Physical Layer
    cables = cables_result["DcimCable"]["edges"]
    print(f"PHYSICAL LAYER: {len(cables)} cables\n")
    for edge in cables:
        cable = edge["node"]
        endpoints = cable["endpoints"]["edges"]
        if len(endpoints) == 2:
            ep1 = endpoints[0]["node"]
            ep2 = endpoints[1]["node"]
            dev1 = ep1["device"]["node"]["name"]["value"]
            int1 = ep1["name"]["value"]
            dev2 = ep2["device"]["node"]["name"]["value"]
            int2 = ep2["name"]["value"]
            cable_type = cable["type"]["value"]
            print(f"   🔌 {dev1}:{int1} ═══[{cable_type}]═══ {dev2}:{int2}")

    # Circuit Layer
    circuits = circuits_result["TopologyPhysicalCircuit"]["edges"]
    circuit_types = {}
    for edge in circuits:
        circuit = edge["node"]
        ctype = circuit["circuit_type"]["value"]
        circuit_types[ctype] = circuit_types.get(ctype, 0) + 1

    print(f"\nCIRCUIT LAYER: {len(circuits)} circuits\n")
    for ctype, count in circuit_types.items():
        print(f"   📡 {ctype}: {count}")

    # Virtual Layer
    virtual_circuits = virtual_circuits_result["TopologyVirtualCircuit"]["edges"]
    providers = {}
    for edge in virtual_circuits:
        provider_node = edge["node"]["provider"]["node"]
        provider = provider_node["name"]["value"] if provider_node else "unknown"
        providers[provider] = providers.get(provider, 0) + 1

    print(f"\nVIRTUAL LAYER: {len(virtual_circuits)} virtual circuits\n")
    for provider, count in providers.items():
        print(f"   ☁️  {provider}: {count}")

    print(f"\n{'='*80}\n")

asyncio.run(main())
EOF
```

### GraphQL Query Building Tips

**Simple Attributes**:

```graphql
name { value }
status { value }
bandwidth { value }
```

**Relationships (Cardinality One)**:

```graphql
provider {
    node {
        name { value }
        display_label
    }
}
```

**Relationships (Cardinality Many)**:

```graphql
devices {
    edges {
        node {
            name { value }
            display_label
        }
    }
}
```

**Polymorphic Queries (Endpoints/Interfaces)**:

```graphql
endpoints {
    edges {
        node {
            ... on DcimPhysicalInterface {
                name { value }
                device {
                    node {
                        name { value }
                    }
                }
            }
        }
    }
}
```

**Filters**:

```graphql
TopologyPhysicalCircuit(circuit_type__value: "dark_fiber") {
    edges {
        node {
            display_label
        }
    }
}
```

### Best Practices

1. **Use heredocs for readability**: Multi-line queries are easier to read and maintain
2. **Start simple**: Query `count` first, then add fields incrementally
3. **Test queries in UI**: Use InfraHub's GraphQL explorer to test queries before scripting
4. **Handle None values**: Always check if relationships exist before accessing nested fields
5. **Use display_label**: Always include `display_label` for human-readable output
6. **Group for summaries**: Use Python dicts to group results by attributes
7. **Quote heredoc delimiter**: Use `'EOF'` not `EOF` to prevent variable expansion
8. **Add progress indicators**: Use `print()` statements to show progress for long queries

### Troubleshooting

#### Error: "Cannot query field 'X' on type 'Y'"

- Field doesn't exist on schema kind
- Check schema: `uv run infrahubctl schema show YourKind`

#### Error: "GraphQLError"

- Invalid GraphQL syntax
- Test query in InfraHub UI GraphQL explorer first

#### Empty Results

- Wrong branch name
- No objects exist yet
- Check: `uv run python -c "..."` with count query

#### ImportError: No module named 'infrahub_sdk'

- Run with `uv run` prefix: `uv run python -c "..."`

## Security Considerations

- Never commit `.env` files or credentials
- API tokens in documentation are demo tokens for local development only
- Avoid introducing OWASP top 10 vulnerabilities (XSS, SQL injection, command injection)
- Validate external inputs at system boundaries

## Code Quality Standards

- **Formatting**: Run `uv run ruff check . --fix` before committing.
- **Type Checking**: Run `uv run ty check .` to ensure type safety.
- **Validation**: Run `uv run invoke validate` to execute all quality checks.

### Markdown Documentation Standards

When creating or editing Markdown documentation files, follow these linting rules:

- **MD031 (blanks-around-fences)**: Always add blank lines before and after fenced code blocks
- **MD032 (blanks-around-lists)**: Always add blank lines before and after lists
- **MD060 (table-column-style)**: Always add spaces around pipe characters in table separator rows

**Examples**:

```markdown
<!-- ✅ Correct: Blank line before code fence -->
**Connection Pattern**:

```text
topology diagram
```

<!-- ❌ Wrong: No blank line before code fence -->
**Connection Pattern**:

```text
topology diagram
```

<!-- ✅ Correct: Table separator with spaces -->

| Column1 | Column2 | Column3 |
| ------- | ------- | ------- |
| Value1  | Value2  | Value3  |

<!-- ❌ Wrong: Table separator without spaces -->

| Column1 | Column2 | Column3 |
|---------|---------|---------|
| Value1  | Value2  | Value3  |

<!-- ✅ Correct: Blank lines around lists -->
**Characteristics**:

- First item
- Second item
- Third item

Next paragraph.

<!-- ❌ Wrong: No blank lines around lists -->
**Characteristics**:

- First item
- Second item
- Third item

Next paragraph.

```markdown

## Common Pitfalls

- **Forgetting to load schema changes**: After modifying a schema, always run `uv run infrahubctl schema load schemas` on the correct branch.
- **Hardcoded paths in tests**: Always use fixtures like `root_dir` to build paths dynamically.
- **Non-idempotent generators**: Ensure generators can be run multiple times without creating duplicate objects or causing errors.
- **Ignoring `order_weight`**: Forgetting to set `order_weight` in schemas can lead to an inconsistent UI.
- **Attribute conflicts in generics**: a node must not redefine an attribute one of its generics provides (no `allow_override`)
- **Missing relationship identifiers**: When multiple relationships point to the same peer, always specify unique `identifier` values
- **Object file HFID errors**: Ensure all values in HFID arrays are strings, even for Number attributes (e.g., `["value1", "3", "443"]`)
- **Generic HFID lookup failures**: Generics used as relationship peers must have `human_friendly_id` defined if data files reference them via HFID strings

## Object File Format Guide

### Overview

Object files are YAML files that provide a declarative way to load data into Infrahub. They work best with idempotent operations and models that have Human Friendly IDs (HFIDs) defined.

**Documentation**: [Infrahub Object File Format](https://docs.infrahub.app/python-sdk/topics/object_file)

### File Structure

All object files must follow this structure:

```yaml
---
apiVersion: infrahub.app/v1
kind: Object
spec:
  kind: NamespaceName  # The schema kind (e.g., DcimDevice, IpamIPAddress)
  parameters:           # Optional
    expand_range: false  # Enable range expansion [1-5] → 1,2,3,4,5
  data:
    - # First object
      attribute1: value1
      relationship1: "HFID-reference"
    - # Second object
      attribute2: value2
```

**Multiple documents**: Separate with `---` to load multiple kinds in one file.

### HFID Requirements

**What is an HFID?**
A Human Friendly ID is a unique identifier for objects, defined in the schema's `human_friendly_id` field. It
allows referencing objects across files without knowing their internal database IDs.

**Critical Rules**:

1. All HFID values must be **strings**, even for Number attributes
2. Multi-value HFIDs use **array format**: `["value1", "value2", "value3"]`
3. Single-value HFIDs can use string format: `"value"`
4. Generic types used as relationship peers **must have HFID defined** for string references to work

**Examples**:

```yaml
# ✅ Correct: All values as strings
health_check: ["http", "3", "3", "2000"]  # Even though rise/fall/timeout are Numbers

# ❌ Wrong: Number values not quoted
health_check: ["http", 3, 3, 2000]  # Will fail HFID lookup

# ✅ Correct: Single-value HFID
device: "dc-1-spine-1"

# ✅ Correct: Two-value HFID
ip_address: ["192.168.1.10/24", "default"]  # [address, namespace]
```

### Relationship Formats

#### Cardinality One

**Option 1**: **Reference existing by HFID (string)**

```yaml
site: "Paris"  # References existing site with HFID "Paris"
```

**Option 2**: **Nested object (will create if doesn't exist)**

```yaml
primary_ip:
  data:
    address: "192.168.1.1/24"
    ip_namespace: "default"
```

**Option 3**: **Nested object with explicit kind (for generics)**

```yaml
network_segment:              # AppComponent.network_segment, peer: the ManagedNetworkSegment generic
  kind: ManagedVxlanSegment   # Specify the concrete type when the peer is a generic
  data:
    name: "c001-web-p"
```

#### Cardinality Many

**Option 1**: **Array of HFID strings**

```yaml
tags:
  - "blue"
  - "production"
  - "critical"
```

**Option 2**: **Array of multi-value HFIDs**

```yaml
health_checks:
  - ["http", "3", "3", "2000"]  # Four-part HFID [check, rise, fall, timeout]
  - ["tcp", "2", "3", "1000"]
```

**Option 3**: **Nested objects (dictionary format)**

```yaml
tags:
  data:
    - name: "blue"
      description: "Blue environment"
    - name: "green"
      description: "Green environment"
```

**Option 4**: **Nested objects (list format with explicit kinds)**

```yaml
devices:
  - kind: DcimPhysicalDevice
    data:
      name: "spine-1"
      role: "spine"
  - kind: DcimPhysicalDevice
    data:
      name: "spine-2"
      role: "spine"
```

### Common Patterns

#### Loading Devices with Nested Components

```yaml
spec:
  kind: TopologyDataCenter
  data:
    - name: "DC1"
      devices:
        - kind: DcimPhysicalDevice
          data:
            name: "dc1-spine-1"
            role: "spine"
            device_type: "N9K-C9336C-FX2"
            status: "active"
```

#### Load-Balanced Components (VIPs are generated)

A `LoadbalancerVIP` is never loaded from a file. The application generator (`generators/lb_vip.py`) produces one
per port of a component with `load_balanced: true`, on the load balancer context serving its instances'
deployment, with the VIP /32, the pool members and the component's `health_checks`. The data file only flags the
component:

```yaml
spec:
  kind: AppApplication
  data:
    - name: "payment-core"
      owner: "C005"
      children:
        kind: AppComponent
        data:
          - name: "web-frontend"
            fqdn: "api.payment-core.c005.demo.local"
            component_type: "frontend"
            ports:
              - "tcp/443"          # One VIP per single port; a range is refused
            load_balanced: true
            network_segment: "c005-web-frontend-local-dc10-p"
            instances:             # The pool members
              - "c005-web-frontend-dc10-1"
              - "c005-web-frontend-dc10-2"
            health_checks:
              - ["http", "3", "3", "2000"]  # Four-part HFID (all strings!)
```

A generated VIP is referenced by its HFID `[load_balancer__name__value, hostname, protocol, port]`, where the
load balancer is the `ManagedLoadbalancerContext` (for example `["DC10-LB1-LB2-ha-PROD", "api.payment-core.c005.demo.local", "tcp", "443"]`).

### Range Expansion

Enable with `expand_range: true` to automatically expand patterns:

```yaml
spec:
  kind: DcimDevice
  parameters:
    expand_range: true
  data:
    - name: "spine-[1-4]"  # Creates spine-1, spine-2, spine-3, spine-4
      role: "spine"
```

**Rules**:

- All expanded fields must have **matching lengths**
- Supports patterns: `[1-5]`, `[10-15]`, `[1,3,5]`, `[A-C]`
- Only works for **string fields**

### Validation and Loading

```bash
# Validate object file format
uv run infrahubctl object validate data/demos/my-demo/

# Load objects into Infrahub
uv run infrahubctl object load data/demos/my-demo/ --branch my-branch

# Load specific file
uv run infrahubctl object load data/demos/my-demo/01_devices.yml
```

### Object File Troubleshooting

**Error**: "Unable to lookup node by HFID, schema 'X' does not have a HFID defined"

- **Cause**: Relationship peer is a generic without `human_friendly_id` defined
- **Solution 1**: Add `human_friendly_id` to the generic schema
- **Solution 2**: Use nested object format with explicit `kind` instead of HFID string

**Error**: **"HFID mismatch" or "Expected X values but got Y"**

- **Cause**: HFID array length doesn't match schema definition
- **Solution**: Check schema's `human_friendly_id` field and ensure array matches exactly

**Error**: **"Type mismatch" or GraphQL validation errors**

- **Cause**: Number values in HFID array not quoted as strings
- **Solution**: Quote all HFID values: `["hostname", "443"]` not `["hostname", 443]`

#### Objects not created

- Verify YAML syntax with `yamllint`
- Check that all referenced HFIDs exist (or use nested objects)
- Load dependencies first (e.g., device_types before devices)

### Object File Best Practices

1. **Use HFIDs consistently**: Define `human_friendly_id` on all schemas where objects will be referenced
2. **Quote all HFID values**: Always use strings in HFID arrays, even for numbers
3. **Load order matters**: Load base data (locations, device types) before dependent objects
4. **Validate before loading**: Always run `infrahubctl object validate` first
5. **Use nested objects for components**: Prefer nested format for tightly coupled data
6. **Explicit kinds for generics**: When referencing generic peers, specify concrete `kind`
7. **Comments for clarity**: Document complex relationships and dependencies
8. **Organize by purpose**: Keep related objects in the same file or directory

## Architecture Decision Records

### Namespaces by technology

**Decision**: Namespaces follow the technology: `Managed` is on-prem, `Cloud` is public cloud. Each concept has one
`Managed` generic (`ManagedNetworkSegment`, `ManagedLoadBalancer`) and one implementation per technology.

**Rationale**:

1. Teams, policies, generators and RBAC are split by technology, not by function.
2. `kind=Cloud*` and `kind=Managed*` select the right resources without extra filters.
3. `Service.*` and `Platform.*` are gone; there is no namespace per functional layer.
4. Operational responsibility is an attribute where a node needs it, not a namespace
   (for example `ManagedCloudProxy.deployment_model`).

### Generics carry shared fields only

**Decision**: A generic holds only what every implementation shares. Technology-specific fields live on the node.

See `.claude/skills/infrahub-greenfield-schema` for the full rules and examples.

## Resources

- [InfraHub Documentation](https://docs.infrahub.app)
- [InfraHub SDK Documentation](https://docs.infrahub.app/python-sdk/)
