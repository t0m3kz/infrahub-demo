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
- **A number or name used in more than one place is defined once in `utils/limits.py`**: design ranges and limits,
  vendor limits, the pool and group names the bootstrap data must define, and the retry policy. The schema and
  `data/bootstrap` stay the source of truth; a constant there is a mirror, and `tests/unit/test_limits_consistency.py`
  names the one to change when they drift. A value used by one module (an algorithm internal) stays next to its code.
- **A platform's config template is `templates/configs/<role>/<platform name>.j2`**: there is no separate template-key
  attribute, so a new platform is a `DcimPlatform` record plus its templates (`tests/unit/test_platform_keys.py`).

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

**When a generator runs**: a generator starts when an object joins its definition's target group (`targets` in
`.infrahub.yml`): a `CoreGroupTriggerRule` (`member_update: added`, `data/events/99_group_triggers.yml`) per definition
that bootstraps on creation, fired by an object created with `member_of_groups` inline or added to the group later;
a re-save of a member fires nothing (measured on 1.11). An object outside the group starts nothing, so an object a
generator must build declares the group (`tests/unit/test_event_rules.py` holds it for the demo data). The `updated`
rules (`data/events/99_actions.yml`) are node triggers with a field match: they fire for every object of their kind,
and the run fails for a non-member ("Target ... is not part of the group"), so they need the membership too.

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
| Managed | On-prem services and the shared generics `ManagedGeneric` (name, description, status), `ManagedGenericDevice` (the individual device edge, `capabilities`, cascades), `ManagedGenericSharedDevice` (the shared device edge, `shared_capabilities`, never cascades), `ManagedGenericInterfaces` (the interface-side `capabilities` edge; its identifier stays `interface_capabilities`), `ManagedNetworkSegment` and `ManagedLoadBalancer`, and `ManagedVRF` (a namespace realized in one topology, a shared kind) |
| Cloud | Provider-hosted resources (public cloud and provider-hosted network services); every resource inherits `CloudResource`, except `CloudNetworkAppliance`, a provider-hosted network function (Equinix Network Edge) that inherits the device generics (`DcimDevice`, `DcimDeviceManaged`) so its interfaces (virtual NICs: no hardware), BGP and HA stay device-shaped, and the two transports `CloudDirectConnect` and `CloudVPNConnection`, which inherit `CloudResource`, `CloudTransport` and only the slim `DcimDevice` (see **Device generics**); a cloud region hosts nothing but the provider's connect devices (`TopologyProviderConnect`). The cloud and SaaS side of a connection is one provider-neutral model (see **Cloud and SaaS connections**): the transport devices (`CloudDirectConnect`, `CloudVPNConnection`, generic `CloudTransport` with `account`, `region` or `saas_region`, `owner`), the peering interface `CloudPeeringInterface` (a `DcimInterface` of its transport), the gateway `CloudGateway` (`hub`, `edge` or `customer`) with `CloudGatewayAttachment` and `CloudGatewayRouteTable`, `CloudVirtualNetwork` and `CloudServiceEndpoint` |
| Topology | Deployments (`TopologyDataCenter`, `TopologyPod`, `TopologyColocationMetro`, ...), design elements, circuits (`TopologyCarrierCircuit` and `TopologySessionCircuit`, whose role is its `circuit_type`: `dci`, `handoff`, `tunnel`, `vpn`; neither has an `owner`, see **Circuit placement**), the one meeting point of every internet connection `TopologyInternet` (bootstrap data, see **Circuit placement**) and the tenant footprints `TopologyCustomer*` |
| Dcim, Ipam, Location, Organization | Devices/interfaces/cables, prefixes/addresses/namespaces, geography, providers/customers |
| Routing | Shared routing objects (`RoutingAutonomousSystem`, `RoutingOSPFArea`, `RoutingPassword`) |
| Security, Loadbalancer, Proxy, App, Virt | Functional domains |

- **One Managed generic per concept, with an implementation per technology**: `ManagedVxlanSegment` and
  `CloudNetworkSegment` inherit `ManagedNetworkSegment`; `ManagedLoadbalancerHA` and `CloudLoadBalancer` inherit
  `ManagedLoadBalancer`.
- **Segments are VXLAN only**: there is no VLAN segment kind. The VLAN of a port is generated per switch (see
  **VLAN rows**), a segment has no manual `vlan_id` and lists its footprints in `customer_deployments`.
  Every segment has a gateway: an authored one is kept, else `add_vxlan_segment` (`ensure_segment_addressing`) allocates
  the segment a /24 from its environment's tenant pool and puts the anycast gateway on `.1`; there is no segment without
  one. The `INFRA` pseudo-customer's segment (`infra-core-dns-ntp-p`, management zone) is such a segment with an authored
  gateway. A segment is pure L2 on the fabric only when a firewall terminates it (`terminated_on`, see **VLAN rows**).
  A fixed VLAN number is only needed towards an outside party, and that is a handoff circuit's `<port>.<vlan>`
  sub-interface, not a segment.
- **Topology.Customer**: Per-facility tenant footprints (`TopologyCustomerDC`, `TopologyCustomerColocation`,
  `TopologyCustomerCloud`) — not a separate `Customer.*` namespace; these inherit `TopologyConnectableLocation`
  alongside the facility nodes so a circuit endpoint is one shared relationship, not a duplicated model. The
  `TopologyCustomer` generic carries only the owner-derived `name`, `infrastructure_orchestrator` and `owner`.
  `TopologyEnvironmentScoped` adds the required `environment`, which every footprint inherits (DC, Colocation and
  Cloud; a cloud footprint's must be its `CloudAccount.environment`, `checks/customer_cloud_account.py` CA3). A cloud footprint
  has exactly one `account` (required, unique: one footprint per account); `checks/customer_cloud_account.py` pins
  that the footprint's owner is the account's owner (CA1) and that its cloud segments use virtual networks of that
  account (CA2).
- **Office**: `TopologyCustomerOffice` is not a `TopologyCustomer`. The owner sits directly on it, its name is
  `{OWNER}-OFFICE` (for example `C005-OFFICE`), it is always PROD (no `environment`) and its parent is the
  "Campus and Office" `TopologyLabel`.
- **Device generics**: `DcimDevice` is slim, `name` (unique across every kind that inherits it, so an interface HFID
  `[device, interface]` is never ambiguous) and `interfaces`: all an interface needs of its device. `DcimDeviceManaged`
  holds the rest of a device (`description`, `role`, `status`, `device_type`, `platform`, `primary_address`, `tags`, and
  the extensions `owner` and `software_image`). `DcimPhysicalDevice`, `DcimVirtualDevice`, `CloudNetworkAppliance` and the
  controllers inherit BOTH; a holder of interfaces that is not a managed device (a cloud transport, THE INTERNET) inherits only
  `DcimDevice`, so it has no role, platform, owner, artifact or capability edge and no query of a managed device sees
  it. A query that reads a moved field through an interface's `device` (typed `DcimDevice`) spreads
  `... on DcimDeviceManaged { ... }`; a list of devices that reads them roots at `DcimDeviceManaged`
  (`DcimDevice: DcimDeviceManaged`, the alias keeps the result key); `TopologyDeviceHosting.devices`, `sdwan_gateway`,
  `managed_devices`, `DcimPlatform.devices` and `DcimSoftwareImage.devices` peer `DcimDeviceManaged` (read `interfaces`
  through `... on DcimDevice`). `DcimProviderPort` is a marker generic (`provider`, `bandwidth`) that `CloudDirectConnect` and `TopologyProviderConnect` list next to the slim `DcimDevice` (a generic cannot inherit a generic): the second is the provider's connect device at ONE place: the Fabric port of a colocation zone, the public (internet-facing) side of a cloud region or a SaaS region. It is a slim device with no account and no owner (it is one per place, not per customer), its place is its mandatory `location` (a `TopologyFacility`: a zone, a region or a SaaS region), the provider's own id of the port is its `external_id`, and its circuits are `internet` carriers (to THE INTERNET) or `virtual_connection`s. It is NOT a Direct Connect: the private connections of a cloud stay in `CloudDirectConnect` and `CloudVPNConnection`. No generator or check of its own: `add_circuit` places a circuit end by its `location`.
- **Circuit placement**: two circuit kinds, both inheriting `[TopologyCircuit, ManagedGeneric, ManagedGenericInterfaces]`:
  `TopologyCarrierCircuit` and `TopologySessionCircuit`. `locations` (peer `TopologyFacility`, max 2, no `min_count`:
  it is enforced on update only) is declared ONCE, on the `TopologyCircuit` generic. The ends are the shared interface
  edge (`capabilities`, peer `DcimInterface`, identifier `interface_capabilities`; an interface has ONLY this edge), re-declared
  on each circuit node with `min_count` 1 and `max_count` 2 (a node that inherits two generics declaring it with a
  different count is refused, measured on 1.11.9; the checks hold the count of two). An end is a physical, LAG or
  virtual interface of ours, of a provider connect (its port or LAG) or a `CloudPeeringInterface` of a cloud transport, so a circuit
  is found from our interface through `capabilities`. A carrier circuit is a pure carrier (fibre, cross-connect, internet access, or a
  provider-delivered `virtual_connection` or `metro_ethernet` service) with no role and no session; its `locations` peer
  `TopologyFacility`: the DC (never a pod, rack or footprint), the colocation
  cage `TopologyColocationZone` or the provider-hosted `TopologyVirtualColocation` (never the metro), the
  `TopologyCloudRegion` (never a zone or `TopologyCloud`), the ISP `TopologyExternalProviders`, the
  `TopologySaasRegion`, the `TopologyCustomerOffice` or THE INTERNET `TopologyInternet` (below). Data lists only the cage; `add_circuit` adds every end
  whose endpoint device's `deployment` is a facility (border leaf -> DC, ISP PE -> ISP),
  when the circuit is created or its ends change: nothing re-runs it on a later `deployment` edit (the check reports
  it). A circuit has no `owner` field: its owner is DERIVED, never stored (`utils/ownership.py` `circuit_owner`): the owner of
  the `TopologyCustomerOffice` among its `locations` (an office's access circuit, `add_sdwan_edge`'s `INET-<office>`, and a
  tunnel); two offices of different owners among the locations raise `CircuitOwnerError`, reported by the
  check. Any other circuit is shared underlay, unowned. `checks/circuit_placement.py` guards what the schema
  cannot, per circuit (a session circuit's rules are selected by `circuit_type`, the schema leaves its type-specific
  fields optional):
  - **Carrier**: exactly two distinct locations, one per end device's `deployment`; its offices are of one owner.
    `checks/carrier_circuit_ports.py`: a port is an end of at most one carrier and both ends
    are physical ports, EXCEPT a `virtual_connection` (a provider port hosts one connection per S-tag, and an end may be
    the `DcimVirtualInterface` NIC of a provider-hosted appliance, or a `TopologyProviderInterface`: the end of ONE
    Fabric connection on the provider's port, unshared, with its S-tag `vlan_id`, C-tag `inner_vlan_id` and
    `bandwidth`, riding on that port through `parent_interface`; the port is a `TopologyProviderConnect`, placed by
    its `location`, the zone, so that zone is the circuit's location); the other types on
    the same port still hold both. A carrier whose far end is such a NIC is a provider-delivered
    `virtual_connection`; a physical `cross_connect` joins physical ports only (our cage edge and a Fabric port or a
    provider's own router).
  - **THE INTERNET** (`TopologyInternet`, the singleton `INTERNET`): the meeting point of every internet connection, "a
    kind of cable or switch for all". It does not change between datasets, so it and its one interface `bus` are
    BOOTSTRAP data (`data/bootstrap/03b_internet.yml`, after the label "Internet" it sits under, next to the ISPs
    `TopologyExternalProviders`); the ISPs, their uplinks and the public sides are dataset data. It is a passive
    `TopologyDeployment`, a slim `DcimDevice` and a `TopologyFacility`: no role, platform, address, VRF, routing or
    artifact (`carries_vrfs` and the readers of managed devices never see it); addresses and prefixes stay in the
    `INTERNET` namespace. Its ONE interface is a `TopologyInternetInterface`, and that KIND lets many carriers end on it
    (`checks/carrier_circuit_ports.py` exempts the kind, not a name). An attachment is an `internet` carrier of its own
    from the uplink port of an ISP router (or the port of the `CloudDirectConnect` that is a region's public side) to the
    bus, so L1 from any ISP reaches any public side through it. The device is a facility itself, so `add_circuit` places
    the bus end by the device (no `deployment`) and a data file lists only the ISP. `utils/internet_object.py`
    (called by `checks/circuit_placement.py`) holds I1 (one `INTERNET`, one interface, of the bus kind), I2 (an
    attachment is an `internet` circuit whose other end is an ISP router or a cloud or SaaS transport, never a DC or a
    cage) and I3 (an ISP router that serves an internet access carrier has an attachment: it applies to EVERY dataset
    since `INTERNET` is bootstrap; an access between two ISP-kind routers, a partner buying from an attached ISP, is
    judged as one). A public cloud or SaaS service is a `CloudServiceEndpoint` with `access: public` and its range as
    `prefix` in the `INTERNET` namespace; its `virtual_network` is an optional plain edge.
  - **Circuit names**: `<end A>-<id>-<end B>`, lowercase; an end is a site (`ktw1`, `par1`), an ISP, `internet`, a SaaS
    region or a cloud as `<provider>-<account id>`. A carrier's id is its operator `circuit_id` (`ktw1-df10001-waw1`),
    which stays its HFID and the key every reference uses, and its display label is the name; a session circuit has
    no `circuit_id`, so its middle part is the type and a leg or the cloud side's id (`ktw1-dci-a-waw1`,
    `par1-dxvif-fgt1a2b3-aws-900000000001`). `add_sdwan_edge` keeps `INET-<office>` as the circuit_id and names it
    `<office>-inet-<isp>`. `data/demos/00_full_topology` follows it; `30_all` keeps its older names.
  - **Carrier provider data** (optional, inventory only, nothing reads it): `circuit_id` also holds the Equinix Fabric
    connection `uuid` or the Megaport VXC `product_uid`; `bandwidth` mirrors the Fabric `bandwidth` and the Megaport
    `rate_limit`; `term_length` (months) has the name and meaning of `CloudNetworkAppliance.term_length` and mirrors the
    Megaport `contract_term_months` or an ISP term (a Fabric connection has none); `commit_rate` (Mbps) is an ISP or MEF
    CIR (Fabric and Megaport have none); `redundancy_group` mirrors the Fabric `redundancy.group` (its
    `redundancy.priority` stays out: the name or description says which is primary). Per-end VLAN tags are not carrier
    attributes (Fabric `vlan_s_tag`/`vlan_c_tag` and Megaport `ordered_vlan` live per end).
  - **Tunnel** (`circuit_type: tunnel`): two locations and two ports, one location a `TopologyCustomerOffice` (its owner
    is the tunnel's, derived; the only type with `registered_prefixes`), no handoff-only field, every end port rides a
    live internet carrier circuit of its own (no contiguity rule).
  - **Interconnect** (`dci`): no office owner, no handoff-only field, two locations and two ports, both ports carried, the
    carriers found at the two ports
    form one contiguous path between the two locations and a single carrier is pure fibre (its two ends are the
    circuit's two ports); each end device has exactly one `overlay` `ManagedBGP`.
  - **Handoff** (`handoff`): no office owner, with an `ip_namespace` (required here, optional in the schema); `password`
    belongs to the handoff and the `vpn` only; one or two locations (both ends may
    sit in one cage); at least one end on a physical device is
    carried (a live carrier circuit or a `DcimCable` on the port or its `parent_interface`, a cable to a patch panel port
    counts: the cross-connect through a panel is cables, see **Patch panels**); the ends are sub-interfaces
    `<port>.<vlan>` with the same VLAN, or a routed physical port (an IP and no VLAN); a `CloudPeeringInterface` end
    (the far end of a cloud leg) fits either shape and needs no carrier of its own, and its `vlan_id` (when set) is the
    VLAN. The session of a cloud leg is authored, `add_virtual_circuit` builds none.
  - **VPN** (`vpn`): an IPsec or GRE tunnel over the internet, one session on a tunnel interface pair; no office owner, no
    `registered_prefixes`, an `ip_namespace`, one or two locations, two end ports. Model only: no IPsec or tunnel
    template exists.
  - **Session circuits (`dci`, `handoff`, `vpn`)**: a port is an end of at most one of them (a `tunnel` is exempt). **Environment class**:
    circuits meeting at a device share a class (prod vs non-prod; a handoff takes its `ip_namespace`'s
    `namespace_type`, a vpn the same, a tunnel is PROD, an interconnect has none).
- **Cloud and SaaS connections**: there is no provider-connection object and no generator that builds a connection's
  circuits or sessions: the cloud and SaaS side of a connection is data, described in the words every cloud shares, and
  a session circuit ends on it. A transport is a DEVICE that holds interfaces, the slim `DcimDevice` only (see **Device
  generics**): `CloudDirectConnect` is the way to the cloud edge, `connection_type` `physical` (a dedicated port) or
  `logical` (a circuit on a partner's or our own port, `rides_on` the transport that holds the port, `service_key`,
  `partner_account`). A transport holds NO port in `data/demos/00_full_topology` (`30_all` keeps the older shape, ports on its transports): the port and the bundle of a connection (`DcimPhysicalInterface`, a
  `DcimLAGInterface` whose members are ports of the SAME device) are the provider's own, on a `TopologyProviderConnect`
  where they physically are: in the cage for a cross-connect (AWS Direct Connect, Google Dedicated Interconnect, Webex:
  the cage edge is cabled to it by a `DcimCable` with the order id as `cable_id`, never a carrier circuit, a hop inside
  one facility is a cable) and in the region for a Fabric `virtual_connection` (the ExpressRoute and Partner
  Interconnect ports, the far end of the carrier). A private and a public device of one place stay two devices, so a
  route never crosses from THE INTERNET into a private peering through a shared device; `CloudVPNConnection` is the
  encrypted transport that holds the tunnels as interfaces. A transport lands in a `region`
  (`TopologyCloudRegion`) or a `saas_region` (`TopologySaasRegion`), exactly one, never in a `deployment`; with an
  `account` it has no `owner`, without one (Webex, Zscaler) it has `saas_region` and an `owner` customer.
  `CloudPeeringInterface` is the L3/BGP termination on the cloud side, a `DcimInterface` of its transport (an AWS VIF, an
  ExpressRoute peering, a GCP VLAN attachment, a VPN tunnel, a SaaS peering) riding on a port or LAG of the provider connect that
  holds it (`parent_interface`, the edge of the marker `DcimSubInterface` it shares with a sub-interface, and with a VM NIC the
  only parent that sits on another device, `checks/interface_parents.py`: the port's and the LAG's
  `sub_interfaces` list it, and it holds its `ip_address` like any interface): `scope` (`private`, `public`, `saas`), `routing`, `vlan_id` or
  `tunnel_index`, the provider's `peer_ip` and `peer_asn`, `auth`, and for a tunnel `endpoint_ip`, `tunnel_mode`,
  `crypto_profile` (`SecurityIPsecProfile`) and `tunnel_key`. A circuit ends on a provider connect's port or LAG, or on a peering, through
  `capabilities` like on any interface; our side is always an interface of one of our devices (a sub-interface, or a
  routed physical port with an IP and no VLAN) and our BGP process, never a cloud object, and the BGP peering of a cloud
  leg is authored (`add_virtual_circuit` builds nothing for a circuit with a cloud end). `CloudGateway` is one kind (`gateway_type` `hub`, `edge` or `customer`); a `CloudGatewayAttachment` joins a
  gateway to EXACTLY one target, a `virtual_network` or a `peer_gateway` (`checks/cloud_connection.py` R1), carries
  `allowed_prefixes` (cloud to us) and sits in `CloudGatewayRouteTable`s of a hub; the prefixes we announce to the cloud
  are the peering's `advertised_prefixes` (public or static only). A cloud region hosts nothing, a SaaS region or a
  virtual colocation hosts `appliances` (`TopologyApplianceHosting`). A provider's own router that a cable reaches is a
  physical device of its `TopologyExternalProviders`. `OrganizationProvider.connection_profile` names the operator's
  vocabulary and is inventory only. The sample is `data/demos/00_full_topology` (one topology in numbered folders by facility type, `tests/traversal/load_topology.py` loads it;
  its README holds the per-operator table, `invoke check-model` and `test_03_full_topology.py` walk it; three sites reach each other and the INTERNET: the DC, one colocation cage `PAR-COLO1` where every cloud and SaaS leg lands (never on the DC edge) and the C900 office with an SD-WAN edge of its own); `checks/cloud_connection.py` (R1 to R9) holds what the schema cannot (the edge derives the cloud side's
  prefixes, see **Border policy**).
- **Path tracer**: `utils/path_trace.py` asks the SERVER for paths between ANY two nodes (`InfrahubPathTraversal`, one
  `traverse_paths` call per layer, `kind_filter` = the layer's row of `LAYERS`: `l1` cabled or carried, `l3` routed).
  A node is `kind:hfid` (HFID parts joined with `/`) or a bare name; nothing is computed locally. ECMP is the set of
  paths at the MINIMUM depth, the longer ones follow. Server limits: `max_depth` 30, `max_paths` 1..100. A chain is a
  row of nodes, which is what `tests/traversal/` (`invoke check-model`, goldens in `tests/traversal/golden/`) asserts.
- **Session circuits** (virtual circuits): `TopologySessionCircuit` is the connection between topologies on which routing
  is generated or recorded; its role is its `circuit_type` (`dci`, `handoff`, `tunnel`, `vpn`), required, never a kind. It has no `owner` field (derived, see **Circuit placement**). The
  carrier kind keeps its own `circuit_type` (the carrier's nature) beside it. `tunnel` (SD-WAN office edge <->
  gateway `eth1`, and the partner tunnel; inventory only, no session, owner = the office among its locations; a member of `virtual_circuits`
  because the generator starts when a circuit joins that group, and the generator logs and stops), `dci` (unowned: a `/31` or
  `/127` eBGP underlay plus a loopback-to-loopback EVPN
  multihop session keyed per circuit, MTU 9214) and `handoff` (our edge sub-interface <-> a party
  device sub-interface in one environment namespace: cloud on-ramps, Equinix/Megaport, the SD-WAN gateway LAN; a `/31`
  from `Handoff-<namespace>-IPv4`, our end the lower address, and one eBGP session with BFD), and `vpn` (an eBGP session over
  a tunnel interface pair to a provider VPN gateway; model only: no IPsec or tunnel template exists). The server refuses
  another length for an identifier it already allocated, so a changed prefix length is reported with its remedy, never
  re-allocated silently. One generator, `add_virtual_circuit`
  (`VirtualCircuitSessionGenerator`, planners keyed by `circuit_type`), builds the sessions of `dci`, `handoff` and
  `vpn` circuits; `add_circuit` only derives carrier locations and fans out to it (one `filters` on `circuit_type__values`). The
  carrier of a session circuit is derived, never listed: the
  carrier circuits (or a `DcimCable` inside one facility) on its port or its `parent_interface`
  (`utils/circuit_underlay.py`). The only role that is an output is `ManagedBGPPeering.peering_role` (`regular`,
  `dci`, `dci-underlay`, `fabric`, `overlay`, `underlay`). The tunnel readers (`edge.gql`, `routing_invariants.gql`,
  `segment_security.gql`) root at `TopologySessionCircuit(circuit_type__value: "tunnel")` and read its `capabilities`.
- **Border policy**: a `regular` session in a PROD or NON-PROD VRF is external unless its far end is ours: the far
  end's BGP process in that peering has `process_role: overlay` (not when the far end device is a
  `CloudNetworkAppliance`, a provider-hosted Network Edge: that hop is external), or the far end is the office's own
  SD-WAN edge (`deployment.lisp_wan_edges`). The device owner is never the signal. A circuit's owner is derived (never stored) and a handoff is unowned; the owner of a
  prefix is the object that registers it (the office's owner for
  `lan_prefixes`, the tunnel's owner for a tunnel `TopologySessionCircuit.registered_prefixes` range such as a B2B partner's,
  `segment.owner` for a segment), and only an `OrganizationCustomer` counts. An external session
  accepts its owners' registered prefixes (`RM-<session>-IN`) and is announced (`RM-<session>-OUT`) what those same
  owners have on-prem or registered behind another external session of the namespace (one office LAN reaches the
  owner's other offices), minus its own IN: the edge passes routes between two external
  sessions of the SAME owner, never across owners, and never back to the session a prefix was learned on (an
  office-hosted segment is registered behind its gateway, so it is not local space). Exact match, one helper
  `utils/ownership.py` `exchange_policies`. A prefix of two owners, or an owner's prefix behind
  two attachment points, is a WARNING in the transform and an error of `checks/routing_invariants.py` rule 8
  (`ownership_conflicts`), one attachment point per SD-WAN gateway and per cloud gateway (`registered_by_attachment`).
  With no owner it accepts and announces nothing. A cloud far end is external: the far end of a cloud session is the
  `CloudPeeringInterface` that ends the same session circuit as our interface, never a device with a BGP process.
  `transforms/helpers/bgp_cloud.py` reads it for the edge and both checks: the neighbor (`peer_ip`, `peer_asn`) and key
  (`auth`), the owner of the transport (its account's owner, else its own `owner` for a SaaS), the prefixes registered
  behind it (the `allowed_prefixes`, else the CIDR blocks, of the gateway attachments reached from the peering's gateway
  through hub hops and route tables) and the peering's `advertised_prefixes` (what we announce to a public or static
  peering, to that session alone). Every destination prefix of an enabled `SecurityPolicyRule` must be on-prem space or
  registered behind an external session (office LAN or hosted segment, tunnel range, cloud network block), in a
  namespace of the source segment's environment: otherwise `checks/segment_security.py` reports "not reachable: no session accepts it"
  (a `destination_segment` or `destination_zone` rule is not checked; the internet is a zone, not a prefix).
  Only the NX-OS edge renders these maps; anywhere else a transform refuses an external session
  (`BaseDeviceTransform.external_policy_platforms`), except a hosted `CloudNetworkAppliance` on cisco_ios/cisco_xe
  (`UNFILTERED_BANNER_PLATFORMS`): it renders an `UNFILTERED` comment above the session and logs a WARNING, because a
  provider-hosted device must not fail its artifact. A handoff peering sends no communities of its own; on the NX-OS
  edge each external session's `RM-<session>-IN` deletes our two routing communities and then sets the owner's customer
  community (the owner of a prefix, above), and `RM-<session>-OUT` strips ours, so an internal tag never leaves and an
  outside party cannot forge one (see **Routing identity**).
- **Internet egress**: an enabled rule to `INTERNET` must be enforced by hardware on every DC and colocation
  footprint of its source segment: `checks/internet_egress.py` requires a complete firewall pair, an INTERNET leg with
  an address per member, the exchange to INTERNET, the tagged border service ports and a `snat_address` per serving
  context (a colocation metro: each zone with firewalls, or a complete provider-hosted pair of its virtual colocation).
  Cloud and office egress is an accepted gap, not checked (the office has a policy artifact, below, but no check). Towards a public SaaS or cloud service the path is the
  L1 of THE INTERNET (see **Circuit placement**) and L3 in the `INTERNET` namespace; no rule of this check judges it.
- **Office firewall policy**: an office has no firewall context (`served_deployments` is for DC and colocation footprints);
  its SD-WAN edge (an appliance with no CLI template, `platform linux`) enforces a stateful policy that the orchestrator
  applies. The artifact `office_firewall_policy` (target group `customer_deployments_office`, JSON, query
  `office_firewall_policy`, `transforms/topology/office_firewall_policy.py`) is built from the segments the office hosts with
  the SAME functions as a firewall rule table (`segment_rule_policies`, `get_zone_policies`): zones, one flat ordered rule
  table closed by one implicit deny, and each segment's `default_action`. A guest Wi-Fi segment is a segment whose only
  permit rule is to `INTERNET`; a corporate one adds the segments and prefixes it may reach. A dedicated guest zone is
  not modelled: the office is PROD, and the exchange zones are tied to the namespaces.
- **DC service tier**: `TopologyCustomerTemplateDC` (referenced by the footprint's `design`, for example `M_DC`)
  holds `dedicated_firewall`, `dedicated_loadbalancer`, `max_segments`, `ports_25g` and `ports_100g` (0 = no
  host-facing ports of that speed). `checks/customer_quota.py` enforces the segment and port numbers; a footprint
  with no `design` is not metered. `add_customer_deployment_dc` (triggered on creation only) links the footprint's
  `serving_loadbalancer_context`; when a re-run moves it to another context (dedicated <-> shared), it re-runs
  `add_app_component` for the footprint's load-balanced components, so their VIPs, R_server and callers' R_client
  rules follow. A `design` change alone re-runs nothing, and a hand edit of the link is not followed up.
- **Colocation firewall service**: a `TopologyCustomerColocation` footprint stays a child of its METRO and names no
  zone. `add_customer_deployment_colocation` builds one shared `ManagedFirewallContext` (with its transit legs and
  exchanges) on the firewall pair of EVERY `TopologyColocationZone` of the metro that has `devices(role: internal-firewall)`; a
  zone without firewalls does nothing and a `TopologyVirtualColocation` (provider-hosted appliances) is never a
  serving zone, so two firewall zones mean two contexts and each zone's firewall enforces the footprint's full policy.
  The footprint links them all in `serving_firewall_contexts` (many); a DC footprint has ONE,
  `serving_firewall_context` (its dedicated or the shared one). Both are declared per kind in
  `schemas/extensions/capabilities/ha.yml` on the identifier of the context's `served_deployments`, and the host
  generator's `_serving_many` picks the flow. A re-run converges the colocation set: a link to a context it no longer
  serves from is removed, the shared context itself stays (untracked, so `delete_unused_nodes` never reclaims it). The transform
  reads the other end, `FirewallContext.served_deployments`, so each firewall places the footprint's rules in its own
  contexts. Locks are per context and a run holds one at a time.
- **Terminated segments**: `ManagedVxlanSegment.terminated_on` (Attribute, one, optional) peers a
  `ManagedFirewallContext`; the reverse is `terminated_segments` on the context (Generic, many; both ends declared in
  `schemas/extensions/capabilities/ha.yml`, identifier `firewall_context__terminated_segments`, narrowed to the concrete
  firewall kind so a load-balancer context cannot be named). The pair of that context is the segment's gateway:
  `segment.gateway` is the pair's floating VIP (the `.1` the segment generator allocates from the tenant pool, like any
  DC segment; no per-member address) and the fabric only bridges it. ONE gate, `utils/segment_addressing.py`
  `is_terminated` / `routed_segment_namespace`, hides the gateway from every fabric reader: the VRF demand (no routed
  row, so no SVI and no VRF instance), `_get_segment_gateways` (no gateway, VRF or L3 VNI), `routed_activations` (no PBR
  or SVI ACL), the overlay and VXLAN L3 instances, the origination, and `is_routed_tenant_segment` (no group or routing
  tag, no SVI protection to demand). The firewall keeps reading `segment_namespace` and `segment_prefix` (its zone
  members, rules and PBR next hops). A query that returns a segment's gateway for the fabric must select
  `terminated_on { node { id } }` (`tests/unit/test_terminated_on_queries.py`), else the segment reads as routed.
  - **Ports**: `add_vxlan_segment` and `add_app_component_segment` tag the border service ports of the context (the
    `capabilities` of the context that are a physical port or a LAG with the firewall service role, read by
    `queries/topology/add/segment_vlans.gql`) with the segment as generated `segments` edges, in the SAME
    reconcile as the component ports (same provenance: an account as `source`, removed only by that account). The ports
    are not host-facing: `checks/customer_quota.py` does not count them. A change of `terminated_on` re-runs
    `add_vxlan_segment` (`data/events/99_actions.yml`).
  - **Firewall side**: the sub-interface is derived, never stored. `transforms/helpers/firewall_terminated.py` names it
    `<uplink trunk>.<vlan>` (the trunk is the `parent_interface` of the context's legs; the VLAN is the segment's row on
    the device at the far end of the trunk's cable, the tag both ends use), gives it the gateway as `virtual_ip` and the
    segment's zone, and `Firewall.transform` renders it through the same interface path as any other. A segment with no
    row on the facing leaf yet, no gateway, or a trunk with no facing device (an unpinned hosted firewall NIC, a colocation
    context with no legs) is left out with a warning. A hosted NIC follows its `parent_interface` to the physical
    chassis port's cable, never an invented direct cable. The derived gateway interface is attached to its serving
    context's policy and routing table, not the appliance's root table. The context never routes a terminated prefix to
    the border leaves
    (`apply_context_routes`), and `get_firewall_static_routes` skips an interface with a `virtual_ip`.
  - **Check**: `checks/segment_placement.py` holds a termination to a gateway inside the segment prefix, DC footprints
    only, each served by the context (`served_deployments`). Demo: C005's `firewalled-dc10` in
    `data/demos/30_all/07_applications/c005/00_segments.yml`, on DC10's shared PROD context (`test_64c`).
- **Footprint segments**: `network_segments` (the reverse of `segment__customer_deployments`) is declared per kind in
  `schemas/extensions/service/segment.yml`: `ManagedVxlanSegment` on `TopologyCustomerDC`, `TopologyCustomerColocation`
  and `TopologyCustomerOffice`, `CloudNetworkSegment` on `TopologyCustomerCloud`, never on the `TopologyCustomer`
  generic. A query reaches it through a per-kind inline fragment (`... on TopologyCustomerDC { network_segments }`),
  not through a generic one. The segment lists footprints only, never its ports: the port-to-segment edge is the port's
  `segments` (see **Interface addresses and layers**), so a reader of a segment's ports queries the ports.
- **Clusters**: a group of hosts that run workloads is `ManagedCluster`, one generic with an implementation per technology
  (`schemas/extensions/service/cluster.yml`). The names say what the cluster is, never whose software runs it: that is its `software_image` (a
  `DcimSoftwareImage`, the release of a `DcimPlatform` such as vsphere, proxmox, nutanix, rke2, eks, aks, as on a device;
  the platform is the image's; the edge is `DcimSoftwareRunner`'s, which a device, a cluster and a `CloudInstance` list in
  `inherit_from`, so an image has ONE reverse edge, `runners`), so one model serves every vendor and a new one is a
  platform and an image record. An image has a `lifecycle` (`approved`, `deprecated`, `end_of_life`) and a `vulnerable` Boolean with a
  `vulnerability_reason` pointer (independent: an image can be both); its display label shows them. `checks/software_lifecycle.py`
  (SL1) reports every runner of a `vulnerable` or `end_of_life` image as an ERROR and of a `deprecated` one as a WARNING;
  no vulnerability is stored, the flag and the pointer are the catalogue's. `ManagedHypervisorCluster` is on-prem: its `hosts` are our `DcimPhysicalDevice` servers and its
  `deployment` is where they stand. `CloudHypervisorCluster` is run by a provider (Equinix Managed Private Cloud is vSphere):
  an `account`, a `location` metro, a `host_count`, no server of ours. `ManagedKubernetesCluster` is on-prem Kubernetes: `control_plane_nodes` and `worker_nodes` are VMs (or servers) of ours and
  `node_segments` the tenant segments they sit in. `CloudKubernetesCluster` is managed Kubernetes: an `account`, a `region`, a `virtual_network` and the cloud `node_segments`. Any cluster may have an `sdn`
  (`ManagedClusterSdn`, declared once on the generic): the software network layer that runs on it, `nsx` on a hypervisor
  cluster or a CNI (cilium, calico, flannel, antrea, ovn-kubernetes, aws-vpc-cni, azure-cni) on a Kubernetes cluster, with its
  `encapsulation`, its `overlay_cidr` (the pod range of a CNI, the TEP pool of NSX), its `service_cidr` and the
  `gateway_nodes`, the VMs that carry it (the NSX Edge nodes). A VM has no cluster edge: it is always hosted on a physical node
  (`DcimVirtualDevice.hosting_device`) and the node is in a cluster only by the cluster's `hosts`, so its cluster is its host's.
  `checks/cluster.py` holds what the schema cannot: CL1 a server is a host of at most one cluster; CL2 the hosts stand in the
  cluster's deployment; CL3 a managed Kubernetes cluster's node subnets belong to its virtual network and that network to its
  account; CL4 the SDN's ranges are networks that overlap neither each other nor the network's blocks; CL5 anti-affinity, the
  instances of one component in a cluster of two or more hosts are not all on one host; CL6 the control plane of an on-prem
  Kubernetes cluster of three or more nodes has no host running more than half of them; CL7 the gateway nodes of a hypervisor
  cluster's SDN stand on hosts of that cluster. Not modelled: VMs of a provider's cluster (Equinix hosts are not ours), the
  Tier-0 and Tier-1 gateways of NSX and their routing, node pools, capacity.
- **Virtual switches**: the L2 element between a VM NIC and the host port (`schemas/extensions/service/virtual_switch.yml`).
  `ManagedVirtualSwitch` belongs to a cluster, has a `switch_type` (standard, distributed, ovs, linux-bridge, hyperv) and its
  `uplinks`, the host ports or bonds (a generic peer without a HFID, so a data file names them as nested objects with an
  explicit kind). `ManagedPortGroup` is a component of the switch: a `vlan_id` and the fabric `segment` it is, and it lists the
  VM NICs in it (`nics`, one-sided: `DcimVirtualInterface` is not extended; a NIC still rides on its host port through
  `parent_interface`). The `segments` edge of an uplink is its allowed VLAN list. `checks/virtual_switch.py`: VS1 the uplinks
  are ports of the cluster's hosts and every host has one; VS2 a VLAN id is used once per switch; VS3 every uplink trunks every
  segment of the port groups; VS4 a NIC is in one port group and its VM stands on a host of the switch's cluster.
- **Fabric design**: `TopologyDataCenter.design` (`TopologyTemplateDC`: S, M, L, XL) and `TopologyPod.design`
  (`TopologyTemplatePod`: S_MIDDLE, S_TOR, ...) are required and hold only the envelope (`max_*`); the rows live in
  `data/bootstrap/14b_fabric_templates.yml`. Every fabric pool size is derived from them by
  `generators/fabric_sizing.py`, and a pod's design must be one of its DC design's `pod_templates`.
- **Suite and rack names are data**: `LocationSuite.name` and `LocationRack.name` are plain, required attributes the
  data spells out (`tests/unit/test_location_names.py`); nothing computes them, so a rename of a suite, a row or a slot
  never renames an object behind the data's back. `suite_name` is gone.
- **Suite capacity**: a `LocationSuite` is the physical capacity: `rows` and `racks_per_row` (both required) are its
  grid. `checks/suite_capacity.py` enforces that every rack sits inside it (`row_index <= rows`,
  `index <= racks_per_row`, where `index` is the slot in the row) and that each hosting suite fits the pod's design
  envelope (`max_rows`, `max_compute_racks_per_row + max_network_racks_per_row`). Racks are data, never generated.
- **Cable attributes**: a generator-created `DcimCable` carries `cable_id` (the short printed label, a hash of the
  endpoint labels), `color` (the TIA-598 jacket colour of its `type`) and `length_cm`, derived from the suite layout
  (`rack_pitch_cm`, `row_pitch_cm`; `generators/helpers/cable_attributes.py`). The length is unset when an end is not
  in a rack or the ends are in different suites. Cables declared in data are not generated and stay unset.
- **Patch panels**: a `DcimPatchPanel` is a passive panel and NOT a device: it inherits nothing (no role, platform,
  artifact, `capabilities` or `shared_capabilities`, no VRF demand, no VLAN row), so no artifact group, transform or
  `DcimDevice` query ever sees it (`schemas/extensions/dcim/patch_panel.yml`). The model is FLAT: the panel lists its
  `DcimRearPatchPanelPort` (`rear_ports`) and its `DcimFrontPatchPanelPort` (`front_ports`) as components, and a front
  port names its rear port (`rear_port`, optional, `common_parent: panel`: the server refuses a rear port of another
  panel). A jack is one front port on one rear port, a cassette several on one MPO-12 rear port. Both kinds inherit only
  `DcimEndpoint`, so a cable ends on a physical interface or on either port kind with no change to `DcimCable`; a
  cross-connect is port - cable - front, rear - cable - port. A panel is unique by its mandatory `rack` and its `name`
  (HFID `[rack shortname, name]`; `PP-01` may exist in every rack). The HFID of both ports is a computed id, because an
  HFID cannot go through two relationships: `<rack>:<panel>:<rear>` and `<rack>:<panel>:F<front>`. A panel names its
  `panel_type` (a Dropdown: `lc-24`, `lc-48`, `lc-96`, `sc-12`, `rj45-24`, `rj45-48` jacks and the MPO-12 cassettes
  `mpo12-cassette-24/48/96`), and the type is the whole layout (`generators/helpers/patch_panel.py` `PANEL_TYPES`, the
  one table; `tests/unit/test_patch_panel_schema.py` holds it equal to the choices): a jack is front N on rear N, a
  cassette gives rear k the fronts (k-1)*F+1..k*F. There is no panel template: an object template copies an attribute
  relationship as it is and cannot point a front port at its sibling (measured on 1.11.9). The generator
  `add_patch_panel` (`generators/topology/patch_panel.py`; the group trigger of `patch_panels`, which every panel
  declares itself, and an `updated` trigger on `panel_type`) builds the missing rear ports, then the missing front ports
  already on their rear port, and pairs a front port left unpaired; it never deletes or re-pairs a port (one the type
  does not have, with another connector or on another rear port is a WARNING), under one lock per panel. Every port is
  saved as an upsert, which the server resolves by `[panel, name]`, so a data file may upsert the same port in the same
  load (a cable end, a used jack given as a nested front port on a nested rear port) and both land on one port.
  `checks/patch_panel_pairing.py` guards the result against the type (`audit()`, the same function): an unknown type,
  a missing or extra port, another connector, an unpaired or misplaced front port. A cable that stops at a panel is data
  to read, not a defect to report. A panel
  always sits between two devices: a circuit end stays an interface (the circuit `capabilities` are untouched) and the
  panel's location is its mandatory `rack` (`LocationRack.deployment` gives the facility) and its place in it is
  `position` (lowest unit) and `rack_face`, declared on the panel with a physical device's meaning (it inherits no
  generic); a panel has no `deployment` and no device type. The `patch-panel` device role is gone. A generated cable
  never ends on a panel port (a panel cable is data). A reader of a cable end meets a panel port as an empty node `{}`
  where it spreads `... on DcimPhysicalInterface` (`queries/topology/cabling.gql`), and must skip it; `far_end_interfaces`
  logs a warning and drops a non-interface far end (following a cable through a panel is not built).
- **Routing identity**: `OrganizationCustomer.routing_tag` and `ManagedNetworkSegment.routing_tag` (Number 1..65534,
  optional) are the numbers behind the standard communities `64496:<customer tag>` and `64497:<segment tag>`
  (`utils/routing_identity.py` derives the strings, no community is stored, and no template re-derives them). They are
  drawn from the Number pools `Customer-Routing-Tag` and `Segment-Routing-Tag`
  (`data/bootstrap/19c_routing_tag_pools.yml`) by generators, never typed in data: `add_customer_routing_tag`
  (the group trigger of `customers`, which every customer object declares) and, for a routed tenant segment,
  `add_vxlan_segment` or the application generator. A
  community is a tag for reading the routing table, NOT a security control: the model-derived prefix-lists stay the
  gate and nothing filters on a community. A customer or segment without a tag renders no community.
  `checks/routing_tags.py` requires a tag on each routed tenant segment and its owner, in range and unique. Not
  `unique: true` in the schema: an optional attribute collides on the second unset value.
- **Routing policy**: `RoutingPolicy` (ordered `RoutingPolicyTerm`s), `RoutingPrefixList` (`RoutingPrefixListEntry`) and
  `RoutingCommunityList` (standard, well-known and large communities) in `schemas/extensions/capabilities/routing_policy.yml`.
  A policy is a SHARED object: many peerings name it per direction (`ManagedBGPPeering.import_policies` /
  `export_policies`), ordered by `priority`, then name, then term `seq`. `origin` says who owns it: a generator writes and
  removes only `generated` ones, a person's `manual` ones are never touched. A term matches a prefix list and/or a community
  list and sets local-preference, MED, communities to add or delete (a list) and a prepend. **A policy sets and tightens, it
  never widens**: `transforms/helpers/routing_policy.py` composes the attached policies with the guard derived from the
  model (the filter of `session["policy"]["filters"]`) so every term matches the guard AND its own match, per address
  family (a route-map names one family's lists); a permit term inherits the session's own sets unless it sets its own; the
  map closes with the guard permit when every policy's `default_action` is permit, else with the implicit deny. While the
  deployment stages the protection (`routing_filter_enforce` false) the guard is not part of the terms. A direction that
  already names a map a template defines (`RM-FABRIC-OUT`, `RM-DCI-IN`: the internal fabric) takes no policy (`skipped`).
  `checks/routing_policy.py` holds the community syntax and the prefix list entries (RP1-RP3), a term after a term
  that matches every route (RP4, ERROR), a policy on no peering (RP5) and a list no term names (RP6, WARNINGs).
  Rendered by NX-OS, EOS and IOS XE (`common/bgp_filter_objects.j2` `bgp_user_objects` and `bgp_user_attachments`): the
  guard is the first, `deny`, term of the user map (a match on the complement list `<list>-OUTSIDE`), and the filter map
  of that direction and family is then not attached (`replaced_by`). Large communities are evidence U.
  SONiC and Dell SONiC render the same in config-DB form (`common/sonic_bgp_policy.j2`: `COMMUNITY_SET`, `ROUTE_MAP`,
  `PREFIX`, `BGP_NEIGHBOR_AF`; every line evidence U) except large communities, which have no known field there: a
  large list is not defined, a term that matches one is left out and a large community a term adds is not set.
  **Transit**: `add_transit_policy` (`generators/topology/transit_policy.py`, the group trigger of `transit_peerings`, which
  every upstream peering of `30_all` declares, guarded by `tests/unit/test_event_rules.py`; `00_full_topology`
  authors the same objects as plain data, held equal to the spec by `tests/unit/test_transit_policy.py`) writes the shared `generated` policies `transit-in` (default route only, local-pref
  below 100, the tag `TRANSIT_COMMUNITY`) and `transit-out` (deny what carries the tag, announce the public space of the
  INTERNET namespace) from the one spec `utils/transit_policy.py` and adds them to the peering; a policy a person attached
  stays, a list entry the public space lost is deleted. There are no fixed transit maps: a transit
  session (an eBGP session in an `internet` namespace) has no derived route protection, so the policies are its only
  filter, and the transform refuses a transit session that names no policy in a direction
  (`UngovernedTransitError`, `BaseDeviceTransform._refuse_ungoverned_transit_sessions`, on every platform). Any
  platform that renders user policies in its VRF address family renders a transit session; the golden is
  `edge_cisco_nxos_internet_bgw`.
- **PBR (steering to the firewall)**: one vendor-neutral model, one behaviour, a renderer per platform. A `RoutingPolicy`
  of `kind: pbr` per customer footprint in a DC (`customer_deployment`), applied on interfaces through the interface
  `capabilities` edge; it has no terms, ACL, bypass or next hop: those are derived when the device is rendered, from the
  footprint's `serving_firewall_context` and the segment policies (`transforms/helpers/firewall_pbr.py`). The VRF pass of a
  device writes the attachments (`generators/helpers/pbr_sync.py`, planned by `pbr_attach.py`: an SVI of a segment whose
  footprint in the device's DC has a context; a manual policy and a border role are never touched) and a leaf steers an
  SVI only when it carries the policy (`pbr_steered_vlans`). Two uses, one contract: PBR of the segment on the ingress of
  its SVI, and PBR 2 at the point where traffic enters the VRF after decapsulation, selecting the prefixes of the
  footprint (the L3VNI SVI on NX-OS; never the underlay port, which sees only the outer header). Same rules on every platform: inspection, allowed bypass, symmetry (both
  directions cross the same firewall) and no re-steering of what returns from the firewall; the attachment and the syntax
  may differ. Each vendor needs its own confirmation of both kinds, the full render of definition and attachment, the
  behaviour with the next hop unavailable, and a documentation or lab proof. A platform that does not meet the contract
  makes the transform refuse the config (`_refuse_unprotected_tenant_svis`), never skip PBR or fall back to plain routing.
  A border leaf steers no segment SVI until its templates render PBR (`DeviceVrfFacts.pbr_footprints_by_name`).
- **Firewall roles**: a device is an `internal-firewall` (east-west and the customers' contexts) or an `external-firewall`
  (the Internet edge of one or more topologies, named by `TopologySegmentHosting.internet_edge_contexts`, see
  `checks/internet_egress.py`). `utils/fabric_roles.py` holds the roles once (`INTERNAL_FIREWALL_ROLE`,
  `EXTERNAL_FIREWALL_ROLE`, `FIREWALL_ROLES` for code that means any firewall); both are rendered by the Firewall
  transform and join the `firewalls` group. The `firewall` role of an INTERFACE (the border port facing a pair) is a
  different dropdown and keeps its name. A DC with an external pair keeps no INTERNET leg on its customer contexts (a
  dedicated one keeps only its own `snat_address`): `generators/firewall_context.py` (`EDGE_CONTEXT`).
- **Route protection**: every BGP session is protected against a neighbour announcing what it must not, internal
  sessions included (external and transit keep `utils/ownership.py exchange_policies`). ONE derivation,
  `utils/bgp_expectation.py` (pure; the transform renders it and `checks/bgp_expectations.py` compares it): a session
  class (underlay, fabric, DCI underlay, overlay EVPN, DCI EVPN, internal VRF, external, transit; an SD-WAN/LISP far
  end is left out) gives, per address family, the prefixes allowed in and out, whether a default route is allowed, and a
  maximum-prefix. Internal classes use RANGES where the space is a pool (underlay: the DC or metro `loopback_pool` with
  `le 32`/`le 128` plus the multisite VIPs in; the own loopback and VIP out, a spine relays the whole pool), the far
  border gateway's own addresses for a fabric or DCI underlay pair, and for EVPN the exact prefixes of the
  environment namespaces the device is a member of (a spine: all of them; the internet namespace as ranges), a default
  route only from a default exit (a border gateway in the internet VRF instance, a border leaf or spine in a tenant one:
  the shared firewall context's `0.0.0.0/0` static; a spine relays it). A family the query data cannot derive
  is "not derivable" and renders UNPROTECTED (reported by the check), never as a deny-all list.
  **maximum_routes is gone**: the limit is derived, never typed: the pool's capacity (already sized with headroom by
  `generators/fabric_sizing.py`) plus the VIPs, the far end's addresses or the allowed set plus
  `BGP_PREFIX_SLACK` (`utils/limits.py`); a loose bound for EVPN (type 3 VTEPs x L2 VNIs, type 5, type 2 as twice the
  IPv4 hosts). `transforms/helpers/bgp_policy.py` composes `session.policy` (one route-map per direction: an attached
  filter carries the local-preference, MED and no-export sets of the maps it replaces) and
  `transforms/helpers/bgp_facts.py` reads the facts off the device query (hosting `loopback_pool`, the
  `multisite-vip` root, `environment_prefixes_*`, `ManagedVRF`, `ManagedVxlanSegment`). **Staged rollout**:
  `TopologySegmentHosting.routing_filter_enforce` (default false, true on every DC and metro of `data/demos/30_all`;
  the precedent is `group_policy_enforce`) renders the prefix-lists and route-maps UNATTACHED with a comment and every
  limit as warning-only; true attaches them. Platforms: EOS, NX-OS and IOS XE render the unicast filters (evidence I);
  only NX-OS renders the EVPN one (U); SONiC and Dell SONiC render everything (U) and only when enforcing; EOS and IOS XE
  render no EVPN filter and EOS no EVPN limit (the check reports them as unprotected, E5); the NX-OS EVPN filter and limit (U)
  are rendered only when enforcing. Every template line carries
  its evidence label. `checks/bgp_expectations.py` (targetless) reports E1 an end announcing what the other would
  filter, E2 a loopback or VIP outside the pool, E3 a connected VRF prefix that is no prefix of its namespace, E4 a
  transit session on a device that is no default exit, E5 a session left unprotected; an ERROR where the hosting
  deployment enforces, a WARNING where it stages: turn the switch on only where it reports no error.
- **Virtual NICs**: the base NICs of a hosted virtual device (a dedicated firewall or load balancer, built from a
  `*_CUSTOMER_*` template) are `DcimVirtualInterface`, never `DcimPhysicalInterface`. Only the physical host is cabled:
  `generators/virtual_nics.py` pins each NIC through its `parent_interface` to the host port of its role (`management`,
  `uplink`, `ha`; n-th by interface order; only cabled host ports qualify for an uplink, and a NIC with no host port
  left stays unpinned). A stale generated pin is removed with RelationshipRemove (SDK save omits a None single
  relationship), and a context sub-interface
  rides on the NIC through `parent_interface` too, so border leaf -> host port -> NIC -> sub-interface is a path in the
  graph. `_resolve_transit_members` follows `parent_interface`, not a name. A controller keeps physical ports;
  a provider-hosted `CloudNetworkAppliance` has no hardware, so its ports are virtual NICs with no host (and no cable).
- **Interface parents**: `parent_interface` is the one "rides on" link, declared once on the marker generic
  `DcimSubInterface` that `DcimVirtualInterface` and `CloudPeeringInterface` inherit (a generic cannot inherit a generic,
  so each kind lists it): a sub-interface's parent is a port, LAG or NIC of its own device, a VM NIC's is the port of its
  `hosting_device` it is pinned to, a cloud peering's is a port or LAG of its transport. It and its inverse
  `sub_interfaces` (declared once on the marker generic `DcimHasSubInterface`, many, peer `DcimSubInterface`: a NIC
  parents several sub-interfaces, a host port several NICs, a transport LAG several peerings; `checks/interface_layers.py`
  reads it as an L3 signal, a port with sub-interfaces is routed; the generators filter on `parent_interface__ids`) carry
  no `common_parent`, so the cross-device rule is `checks/interface_parents.py` (rooted on `DcimSubInterface`): a parent
  on another device is an ERROR unless the interface is a NIC of a `DcimVirtualDevice` pinned to a port of its
  `hosting_device`, a NIC's parent is a physical port, and an uplink's host port is cabled. The transforms render a
  `parent_interface` only for a parent on the interface's own device (`transforms/helpers/addressing.py` `sub_interface_parent`; the
  queries select both `device` ids). The VLAN tag of a sub-interface is its `vlan_id` and `inner_vlan_id` (QinQ C-tag), declared once on
  `DcimSubInterface` (unset: untagged); `<parent>.<vlan>` is only the default name the generators give, nothing parses it.
  `common_parent: device`
  stays on the LAG pair `lag` / `member_interfaces`; `common_parent` works only on a node with a `kind: Parent`
  relationship and a peer with one (the interfaces' `device`, `CloudNetworkSegment`'s `virtual_network`), every other
  "same parent" rule is a check.
- **Interface addresses and layers**: `ip_address` is declared once, on the marker generic `DcimHasIPAddress`
  (inherited by a physical port, a LAG, a virtual interface and a cloud peering interface, not a console port): an
  Attribute with no cascade (a multisite VIP is one address on two loopbacks), so an address goes with its run's tracking or with decommission's
  explicit deletes, never with its port; a hand-deleted device leaves its port addresses behind. A switch port's L2
  membership is the typed edge `segments` (on the marker generic `DcimSwitchedInterface`, a physical port or a LAG;
  peer `ManagedVxlanSegment`, many) is the ONE edge between a port and a segment, one-sided: the segment has no list of
  its ports, a reader asks the ports (`segments__ids`, `segments__isnull: false`), and a change of it raises no event.
  Physical ports and LAGs only; a virtual interface is never assigned to a segment by anyone. The edge carries its
  provenance, the relationship `source` property (a `CoreAccount`; `generators/helpers/edge_source.py` is the one writer,
  a raw `RelationshipAdd` with `_relation__source`; read with `edges { properties { source { id } } node { id } }`, which
  `clean_data` drops, so the reader takes it from the raw response): a person sets an edge from the port with no source,
  the segment reconcile writes the ports its components derive with its own account (the client token's, `AccountProfile`,
  asked once per run) as the source. A segment is on no interface's `capabilities`. The layer of a port is its content, not its kind: a
  segment or a firewall/load-balancer context means switchport, an address, sub-interfaces or a routed capability
  (BGP or OSPF peering, routed exchange, interconnect, tunnel or handoff circuit) means routed;
  `checks/interface_layers.py` refuses both on one port (R1), or on a LAG member (R2); a border service port tagged with a
  context AND a terminated segment is a switchport on both counts; a segment on a switch port's `capabilities` is
  impossible by schema (`ManagedVxlanSegment` is not on the interface edge).
- **VLAN rows**: a VLAN ID is local to a switch. There is no VLAN domain object and no marker generic: the row
  points straight at its device. The chain is segment -> `ManagedSegmentDeployment` (the realization in a DC, metro or
  office, with the VNI) -> `ManagedSegmentVlan` (the realization on ONE switch, with the `vlan_id`): the row is a
  `Parent`-mandatory child of the deployment its switch sits in (`device.deployment` is the hosting parent or one of its
  pods and zones) and goes with it (`on_delete: cascade`), so the VLAN and the VNI of a port come from one lineage; its
  `device` is an Attribute (mandatory), unique on [segment_deployment, device] and [device, vlan_id]. The row is an
  output and has no port: a port is given a SEGMENT (the port's `segments` edge), never a VLAN, and the VLAN of
  a port is found by port -> device -> row of the segment. **MLAG pairs**: the two peers of a `ManagedMLAG` each have
  their own row for a segment and MUST carry the same `vlan_id` (a dual-homed port-channel is one L2 broadcast domain).
  The allocator holds it: the segment reconcile gives every device with a port of the segment AND its MLAG peers (even
  a peer with no port of the segment: a dual-homed LAG needs the tag on both) a row, takes the pair's locks in sorted
  order and gives the pair one id (the peer's existing id when it is free, else the lowest id free on both); a row's id
  is never re-chosen. A late pairing whose two rows already differ is NOT renumbered: the reconcile logs a WARNING and
  `checks/mlag_domains.py` M4 fails until a person fixes it. M4 also reports a peer without a row where its partner has
  one; M5 requires, for each `lag_id` of an MLAG, exactly two port-channels, one per member, with equal `lacp_mode`
  and `minimum_links` (the bond's grouping is the pair (MLAG, `lag_id`); the peer-link is excluded). The templates render
  no `mlag <id>` / `vpc <id>` on the host-facing port-channels today (only the domain-level block and the peer-link).
  Deleting an MLAG never orphans a row. The segment owns its port tags and its rows
  (`generators/topology/segment_ports.py`): `add_vxlan_segment` and `add_app_component_segment` run the same reconcile,
  by provenance: it removes a port edge ONLY when its `source` is the generator's account and no `AppComponent` instance
  lands on the port any more (an instance moved, a component left, the segment has no component), and it never removes or
  changes an edge with no source (set by a person from the port), whatever the components say. A person's edge on a port
  that is also derived stays as it is (it is not re-stamped, so it survives the component leaving). The edge is one-sided and
  no trigger follows it: a port a person adds gets its VLAN row at the next run of either generator on the segment. A switch with no deployment of the segment, or a pair whose members map to two deployments, is a WARNING and its
  rows are left alone (the next run converges once the deployment exists). The rows are untracked and their deployment
  tracked: a deployment that is not re-saved by its run is deleted with its rows; the rows of a device that is gone or
  no longer needs the segment are deleted by the next reconcile (check rule S2 reports them meanwhile). **VLAN scope**: `ManagedVxlanSegment.vlan_scope` is `local` (the default: each switch has its own id, the VNI is the identity) or
  `shared` (one id on every switch, for L2 trunked between switches that cannot do VXLAN). A shared segment draws the ONE
  VNI of the `GLOBAL-L2VNI` pool like any stretched segment (even in one DC), and its id is derived from it:
  VLAN = 2500 + (VNI - 40000), so the first 500 numbers of that pool (VNI 40000-40499) give the shared band 2500-2999,
  above the local band 100-2499 (`utils/limits.py`, `utils/segment_vlan.py` `shared_vlan_id`; a test ties the start to
  the pool). A third DC joins with one more realization under the same VNI and so the same id. The generator gives a
  shared segment that id on every row and fails when a switch already uses it; `checks/shared_vlan.py` (SV1) reports a
  row that differs, a VNI outside the band or two VNIs. A stretched segment (`stretch_scope` not `local`) is realized on every VTEP of its hosting
  parents (leaves, ToRs and border gateways, `VTEP_ROLES`), with or without a port of it: each has a row and an SVI carrying the
  same anycast gateway, so a host keeps its gateway when it moves between the DCs.
  A routed row (a gateway, and no `terminated_on`: a terminated segment is pure L2 on the fabric) is realized on its switch as ONE SVI: a `DcimVirtualInterface` with role
  `svi` named `Vlan<vlan_id>`, a Component of the row (`svi`, `on_delete: cascade`), with no address (the anycast
  gateway is `segment.gateway`) and with `vrf` pointing at the `ManagedVRF` of its device's topology; the SVI is not a
  port. `generators/helpers/svi.py` writes it in the VRF pass of the segment reconcile, upserted by (device, `Vlan<id>`), and
  the transform renders `vrf member` from it. The L3 VNI SVI (`ip forward`, `3900 + TRANSIT_SLOT[namespace_type]`,
  `utils/exchange_transit.py` `l3vni_svi_vlan`) and the transit-leg SVIs of the firewall and load-balancer contexts stay
  derived, not objects.
- **Routing authority**: IPAM objects and allocations use the builtin `default` namespace; that identity never chooses
  a routing table. `SecurityZone.name` is the CLI VRF name (`PROD`, `NONPROD`, `INTERNET`, `MANAGEMENT`), and its
  `vrf_type` and `l3_vni` are authoritative. `utils/routing_domain.py` resolves native interface, SVI, circuit, segment
  and standalone prefix owners, rejects conflicts, and requires missing tenant ownership to fail closed. Genuine
  underlay and loopback objects have no zone and create no managed VRF demand. Direct query execution uses
  `utils/graphql_queries.py` to assemble referenced shared fragments.
- **VRF instances (`ManagedVRF`)**: one per (topology, security zone): its `deployment` is a `Parent` to the hosting
  deployment (a DC, a colocation metro or an office, `TopologySegmentHosting`; a pod's or a zone's devices are hosted
  by its DC or metro, `utils/segment_addressing.py` `hosting_deployment`), deleted with it (`vrfs`, `on_delete:
  cascade`); unique on [deployment, security_zone], HFID [deployment name, zone name], named
  `{deployment lower}-vrf-{zone lower}` (`dc10-vrf-prod`), the scope of its route-target. Its members are the
  devices of that topology that route in the namespace: `shared_devices` (the shared device edge, reverse
  `DcimDeviceManaged.shared_capabilities`, no cascade), written edge by edge with RelationshipAdd/RelationshipRemove,
  never as a list (a new instance is created with its first member in the create data); the zone lists its instances through
  `SecurityZone.vrfs`. `l3_vni` stays on the zone (one value: a stretched VRF and the global INTERNET RT need it);
  the instance has no duplicate VNI field; RD, RT, SVI VLAN and EVPN vs VRF-lite
  are derived. Membership is demand-driven, never pre-provisioned: a device is a member for exactly the non-`default`
  namespaces it routes in (`utils/vrf_demand.py`, the one derivation): the gateway namespace of each
  routed row of its own, its SVIs' VRFs, the namespaces of the addresses on its own interfaces (handoff /31s, WAN
  exit sub-interfaces, an upstream transit address) and the legs of the contexts tagged on its border service ports.
  Only our routing devices carry one (`carries_vrfs`: leaf, tor, access-leaf, border-leaf, border-spine, edge,
  access-switch, distribution-switch, physical or a `CloudNetworkAppliance`): never a spine, an L2 leaf, a firewall or a
  load balancer (their VRF is the context), a controller, or a party's device (a provider stub, an appliance of a cloud
  region, a device of an external provider). A device with demand but no hosting deployment gets nothing (check rule g, ERROR).
  `generators/helpers/vrf.py` is the only writer (untracked; lock order: the segment lock, the device locks
  `vrf-device-<id>`, then the instance locks `vrf-<deployment>-<namespace>`, the latter only to create or delete; never
  under a context lock): the segment reconcile, the context legs (`customer_facility`), `add_virtual_circuit`, the office
  WAN exit and `add_circuit` (the data-authored internet addresses of the cage edges) call it; it joins before the
  dependent write and leaves when no demand remains (after the SVIs); an instance is deleted when its last member and
  its last SVI are gone (every pruning pass sweeps its topologies, and the pod decommission sweeps the DC), because on
  NX-OS `no vrf context` moves the member addresses to the default table. A device's VRF set in the transforms is its
  memberships (`shared_capabilities` of kind `ManagedVRF`, `utils/vrf_demand.py` `device_memberships`, read by the
  renderers through `device_vrfs`); one is realized as an L3 VNI (LISP instance-id) when it has an SVI on the device or is its default exit (a
  border gateway's INTERNET, an office border's WAN exit), else VRF-lite. A routed segment, SVI, address, session or leg
  in a namespace the device has no instance of raises `VrfRealizationError` (a hosted `CloudNetworkAppliance` on
  cisco_ios/cisco_xe renders a `VRF-UNREALIZED` banner and logs a WARNING instead). A caller writes the dependent
  address inside `vrfs_held(needs)`, which ensures the instances and keeps the device locks (not re-entrant) until the
  write is done, so a concurrent pruning pass cannot delete a fresh instance. An SVI is never written over an interface
  of another role: a desired `Vlan<id>` whose name is taken (the MLAG `Vlan4094`, a hand-made VlanN) is skipped with a
  WARNING. Accepted gaps, converged by the next `reconcile_vrfs` of the device (any segment, circuit or context run that
  touches it) and meanwhile reported by `checks/vrf_instances.py` as a member without demand (WARNING): a deleted
  segment is not reconciled (its deployments, rows and SVIs go by the schema cascade), a footprint removed from a
  segment loses its deployment in the cleanup after the segment's own reconcile, and an edit of a device's
  `deployment` or `role`, or a port taken off a handoff circuit, re-runs nothing. The check also reports demand
  without a membership, a member of another topology's instance, a device with demand and no hosting deployment
  (ERRORs), an instance with no member (WARNING) and the SVI rules.
- **What the schema refuses, measured on a server (1.11.9)**: `parameters` (min/max of a Number) and `max_count` are
  enforced on create and on update; `min_count` only on update (a member removed below the minimum is refused, a
  domain created with one member of a `min_count: 2` is accepted); `optional: false` refuses an omitted relationship
  but accepts an explicit empty list for a "many". So an exact pair (an HA domain, a circuit's two ends) is capped at
  two and cannot be shrunk, and a check still has to catch the one-member create. Narrowing a relationship's `peer` to a
  concrete kind makes `id` non-null in its GraphQL type (`String!`) while a sibling kind with a generic peer returns
  `String`, and a query that selects that field across kinds is then refused at repository sync ("Fields ... conflict");
  narrow to a generic instead (`TopologyApplianceHosting` for an appliance's deployment). The repository sync of
  `test-integration-fast` catches that conflict.
  A **Parent** relationship must be mandatory (no optional Parent, so no uniqueness over an optional relationship: use a
  plain edge when the peer is optional). `infrahubctl schema load` is ADDITIVE: it refuses a change of `inherit_from`,
  `hierarchy` or `parent` on a kind that has instances, so such a change needs a rebuilt stack
  (`uv run invoke clean-testcontainers`, or `check-model --rebuild`).
- **MLAG peer pair uniqueness is an attribute, the rest are checks**: a uniqueness constraint cannot use a many
  relationship, so the pair is mirrored into the unique Text attribute `ManagedMLAG.peer_devices` (the sorted member
  names joined with a comma, `mlag_peer_devices` in `utils/segment_vlan.py`), written by `generators/domain_pairing.py`.
  `checks/mlag_domains.py` enforces M1 (a device is in at most one domain: the attribute alone does not stop a-b plus
  a-c), M2 (exactly two distinct members: `min_count` is only enforced on update), M3 (`peer_devices` equals the
  sorted member names), M4 (both members have a VLAN row with the same `vlan_id` for every segment either has one for,
  see **VLAN rows**) and M5 (each `lag_id` has exactly one port-channel per member with equal `lacp_mode` and
  `minimum_links`).
- **What a deployment hosts is typed by its kind**: a device's `deployment` is its host's, and each kind of host has a generic
  of its own, so the picker and the schema refuse the rest. `TopologyDeviceHosting` (DC, pod, colocation zone, office,
  external provider) lists the physical devices and controllers (a provider's physical router lives in its
  `TopologyExternalProviders`, never in a region);
  `TopologyApplianceHosting` (virtual colocation, SaaS region, cloud region) lists its `appliances` (`CloudNetworkAppliance`, identifier
  `topology__appliances`); `TopologyCloudHosting` (customer cloud) lists its `instances` (`CloudInstance`, the EC2 of the
  tenant). A virtual device keeps `deployment` on the `TopologyDeployment` generic: it runs for a customer footprint. The
  colocation metro hosts nothing and cables nothing. `LocationRack.deployment` (peer `TopologyRackHosting`: a pod or a colocation
  zone) is named like the device one, and pairs with `TopologyRackHosting.racks`.
- **Service context and HA names**: lowercase, the owner or `shared` first, the tenant namespace class (`prod`, `non-prod`) last.
  A shared firewall or load-balancer context is one per (cluster, tenant namespace): `shared-{cluster}-{class}` on the physical
  HA `{first}-{second}`, so PROD and NON-PROD never meet on one context. A dedicated pair is `{owner}-{first}-{second}-ha-{class}`
  (HA domain) with the context `{owner}-{first}-{second}-{class}` on it, its members `{owner}-{physical}-{class}`. The HA and the
  context are nodes of one generic and the server resolves an upsert by name, so they never share one.
- **Load balancer pool members are servers, not addresses**: `LoadbalancerVIP.pool_members` peers `AppInstance` (physical or
  virtual device, cloud instance); a member's address is its `primary_address`, or the `private_ip` of a cloud instance.
  An on-prem server with none gets one reserved in its component's segment prefix by the application generator (kept as its
  primary address), a server that has one keeps it. The VIP has no link to the application: the generator derives it from
  `AppComponent.instances`. A cloud component (`CloudLoadBalancer`) is layer 4 or layer 7 by `AppComponent.lb_layer`
  (`l4_cloud` / `l7_cloud`); a layer 7 listener is `https` with `AppComponent.lb_certificate` (port 80 is always `http`), else
  `http`, and only tcp can be layer 7. A cloud VIP has no `vip_ip` (the provider assigns the endpoint, `dns_names` of the load
  balancer) and its targets are the instances themselves. `ManagedLoadBalancer.vips` lists the VIPs of every implementation.
- **Rule destinations**: a `SecurityPolicyRule` targets a segment (`destination_segment`, narrowed by prefixes or
  addresses), prefixes, addresses, a group tag or a zone (`destination_zone`). The internet is the `INTERNET`, not a
  segment: a rule to it has `destination_zone: INTERNET` and nothing else (the firewall renders `to INTERNET`,
  destination any; the leaf ACL denies `NON_INTERNET_PREFIXES` before the permit to any). Only that zone is accepted,
  and `checks/segment_security.py` refuses a zone next to a segment or group tag (a zone qualifies a prefix or address selector).
- **A device's functions are capabilities**: anything a device (or a cloud instance) does or is configured with beyond its
  identity is a capability object on the device edge, never an attribute or a relationship of the device kind. The device
  kind keeps identity and inventory only: name, role, status, platform, type, software image, owner, deployment, address,
  interfaces. A function of one device, or a setting specific to it, is an individual capability (`ManagedGenericDevice`,
  cascades with the device: a BGP or OSPF process, an MLAG domain, an HA pair, `ManagedPBR`); one object several devices
  are members of is a shared capability (`ManagedGenericSharedDevice`, never cascades: a VRF instance, a service context,
  an NTP, SNMP or AAA profile). An interface-side object (on `ManagedGenericInterfaces`) exists only when the interface
  has a setting of its own (`RoutingOSPFInterface`: metric, mode, authentication); a binding that carries no setting is
  derived when the device is rendered, never stored (the steered SVIs of PBR, the legs and border ports of a context:
  `utils/device_services.py`). So a transform reads what a device does in ONE place, its `capabilities` and
  `shared_capabilities` (a typename filter), nested under the device, and derives the rest from that and the device's own
  data. A cloud instance holds none today; the first one that needs a function gets the device end of the capability
  edges through a glue generic it shares with `DcimDeviceManaged` (relationships only, see **Generics carry shared
  fields only**).
- **Capability edges**: a capability reaches devices through ONE of two device-edge generics (a kind inherits exactly
  one, `tests/unit/test_capability_edge_names.py`) and interfaces via `ManagedGenericInterfaces` (inherit it only when
  something reads the interface edge). Both device edges sit on `DcimDeviceManaged`:
  - **Individual**, `ManagedGenericDevice`: `DcimDeviceManaged.capabilities` (identifier `capabilities`) cascades with the
    device. Anything that must die with its device stays here: a BGP or OSPF process, an MLAG domain, a firewall HA pair.
  - **Shared**, `ManagedGenericSharedDevice`: `DcimDeviceManaged.shared_capabilities` (identifier `shared_capabilities`,
    `on_delete: no-action`, explicit) never cascades; the object's members are its `shared_devices`. Deleting a member
    never deletes the object and deleting the object leaves its devices. `ManagedVRF` and the service contexts
    (`ManagedFirewallContext`, `ManagedLoadbalancerContext`, on the thin generic `ManagedSecurityContext`) are shared kinds
    (an SNMP profile, NTP or AAA would be the next). A context's lifecycle owner is its hosting deployment (a `Parent`,
    `TopologySegmentHosting.security_contexts`); its members are the devices that enforce it, and it holds no HA edge: a
    firewall finds its `ManagedFirewallHA` through its own capabilities. A new shared kind is only a node with
    `inherit_from: [ManagedGeneric, ManagedGenericSharedDevice]`; it declares no device relationship of its own.
  - Rules of a shared kind: members are written from the shared side, edge by edge (RelationshipAdd/RelationshipRemove),
    never as a saved list, because several generators attach devices to one object; it has a lifecycle owner (a
    `Parent`, like `ManagedVRF.deployment`, or a data file); the transforms and checks read
    `shared_capabilities { __typename ... on <Kind> {...} }` once, inside `... on DcimDeviceManaged` (the field is not on
    `DcimDevice`), in `DeviceCapabilitiesOnDeviceFields`, and filter on the typename (`device_memberships`).
  - `ManagedVRF`'s SVIs reach it through `DcimVirtualInterface.vrf`. A segment is not on the interface edge: a switch
    port's membership is `segments` (see **Interface addresses and
    layers**); the kinds left on `ManagedGenericInterfaces` are contexts, the routed exchange, peerings and OSPF.
    The circuits are on it too: their two ends are their `capabilities` (see **Circuit placement**).

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
- The order a user sees is the merge of every generic a kind inherits plus its own fields, not the order of one file.
  Weights are unique per kind after inheritance, `name` comes first, then `description` and `status`. Give every
  attribute and relationship an explicit weight: the server numbers an unset one itself (1000, 2000, ...) and it ties.
  `CoreArtifactTarget.artifacts` sits at 1000, so a kind that inherits it keeps its own fields off 1000 (`name` at 900).
  When two generics declare the same field the first in `inherit_from` wins. A weight-only change loads in place on a
  running stack (`uv run infrahubctl schema load schemas`); a changed relationship direction does not.

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
- `tests/smoke/test_golden_structure.py` lints the rendered goldens: a new platform template must keep it green.
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
stack at a time. Two loops:

- **Fast (minutes)**: `uv run invoke check-model` (schema plus `data/demos/00_full_topology` on a throwaway `tv-*` branch of
  the :8100 stack, then the tracer asserts of `tests/traversal/`; `--rebuild` for a clean stack; the pairs run two at a
  time, `--pair <regex>` picks some, `--reach` runs only the far-end matrix: one shortest path per pair, no goldens) and
  `uv run invoke test-integration-fast` (`test_01` setup, `test_02` repository sync, `test_03` the same topology and
  traversal). Run it BEFORE the full subset for any schema, query or generator change: `test_02` registers every query
  against the live schema, so a field the schema rejects (`Cannot query field`, `Unknown type`, a "Fields ... conflict")
  fails there, where the full subset costs tens of minutes (the task worker log names the field).
- **Full 30_all subset**: `test_01, 02, 59, 60, 61, 62, 64, 65, 66` (`62` is `test_62`, `62b`, `62c`; `64` is `test_64`,
  `64b`, `64c`). `test_65` (idempotency) is the last ~20 of its ~55 minutes, `test_66` (circuit lifecycle, mutates the
  branch) runs last. The fast loop loads its topology onto the stack's `main`: `uv run invoke clean-testcontainers` first.

| Change touches | Run |
| --- | --- |
| Schema, queries, checks of the model, `data/demos/00_full_topology` | Fast: `check-model` and `test-integration-fast` (`01, 02, 03`) |
| Generators (including their helpers), `data/bootstrap`, `data/demos/30_all`, queries used by generators | Fast, then the full subset **including `test_65` and `test_66`** (a generator's cleanup is only proven by `test_66`) |
| Templates, transforms, checks and their validation queries | Fast, then subset `01, 02, 59–64` (no `test_65` or `test_66`) plus the smoke golden diff |
| Docs, tests only | Unit + smoke |

Queries are validated by the smoke tests: every query under `queries/` has a `syntax_check` entry in
`tests/smoke/test_graphql.yml` (guarded by `tests/unit/test_smoke_query_coverage.py`), run by `uv run invoke validate`.
The test stack serves on port 8100 (`--server-port` moves it).

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

    kinds = ['DcimPhysicalDevice', 'DcimCable', 'TopologyCarrierCircuit', 'TopologySessionCircuit']

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
            TopologyCarrierCircuit {
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

    circuits = result["TopologyCarrierCircuit"]["edges"]
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
            TopologyCarrierCircuit {
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
            TopologySessionCircuit {
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
    circuits = circuits_result["TopologyCarrierCircuit"]["edges"]
    circuit_types = {}
    for edge in circuits:
        circuit = edge["node"]
        ctype = circuit["circuit_type"]["value"]
        circuit_types[ctype] = circuit_types.get(ctype, 0) + 1

    print(f"\nCIRCUIT LAYER: {len(circuits)} circuits\n")
    for ctype, count in circuit_types.items():
        print(f"   📡 {ctype}: {count}")

    # Virtual Layer
    virtual_circuits = virtual_circuits_result["TopologySessionCircuit"]["edges"]
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
TopologyCarrierCircuit(circuit_type__value: "dark_fiber") {
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
- **Code under `data/`**: no `.py` file under `data/demos` (nor `data/bootstrap`); data is YAML, anything that computes
  belongs in `generators/`, `utils/` or `tests/`.
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
load balancer is the `ManagedLoadbalancerContext` (for example `["shared-lb-dc10101-lb-dc10102-prod", "api.payment-core.c005.demo.local", "tcp", "443"]`).

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
