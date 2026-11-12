# Infrahub Demo: Design-Driven Network Automation

This repository models data center, colocation, cloud, SaaS and office networks in
[Infrahub](https://docs.infrahub.app) and generates them from design data. Schemas define the data model,
generators build the topology, transforms render device configurations, and checks validate the result before a
change is merged.

Two ideas drive the project:

- **Design-driven topology.** A fabric is described by its design (size, pod layout, routing strategy, naming
  convention). Generators derive the devices, cabling, addressing and routing from it, so a new pod, rack or switch
  is a data change, not a manual build.
- **Application-driven infrastructure.** An application team declares what its application needs (for example
  "checkout talks to payment on tcp/443"). Generators derive the firewall rules, proxy configuration, security policy
  and routing from that one declaration, consistently, on a branch that is reviewed before it is merged.

## Contents

- [Requirements](#requirements)
- [Repository Structure](#repository-structure)
- [Quick Start](#quick-start)
- [Scenarios](#scenarios)
- [Loading a Scenario](#loading-a-scenario)
- [Testing](#testing)
- [Troubleshooting](#troubleshooting)
- [Further Documentation](#further-documentation)
- [License](#license)

## Requirements

- Python 3.11 to 3.14
- [uv](https://docs.astral.sh/uv/) for dependency management
- Docker (Infrahub stack and integration tests)
- Infrahub 1.10 or later (the registered flattened queries need 1.12; see [AGENTS.md](AGENTS.md))

## Repository Structure

| Path | Content |
| ---- | ------- |
| [`schemas/`](schemas/) | Data model: base schemas (`base/`) and domain extensions (`extensions/`) |
| [`data/bootstrap/`](data/bootstrap/) | Reference data every scenario needs: locations, providers, platforms, device types, templates, pools, designs |
| [`data/events/`](data/events/) | Event rules and actions that start generators |
| [`data/demos/`](data/demos/) | Demo scenarios (see [Scenarios](#scenarios)) |
| [`generators/`](generators/) | Topology and service generators (DC, pod, rack, segments, circuits, applications, change requests) |
| [`transforms/`](transforms/) | Python transforms that build device configuration payloads |
| [`templates/`](templates/) | Jinja2 configuration templates, one per role and platform (`templates/configs/<role>/<platform>.j2`) |
| [`checks/`](checks/) | Validation checks run on proposed changes |
| [`queries/`](queries/) | GraphQL queries; `queries/flat/` holds the generated flattened copies that are registered |
| [`menu/`](menu/) | Infrahub navigation menu |
| [`utils/`](utils/) | Shared helpers and limits (`utils/limits.py`) |
| [`tests/`](tests/) | Unit, smoke and integration tests |
| [`.infrahub.yml`](.infrahub.yml) | Registry of generators, transforms, checks, artifacts and queries |
| [`tasks.py`](tasks.py) | Invoke tasks for setup, loading and testing |

## Quick Start

A preconfigured development container is available in [`.devcontainer/`](.devcontainer/).

### 1. Install dependencies

```bash
uv sync --group dev
```

### 2. Set the environment

```bash
export INFRAHUB_ADDRESS="http://localhost:8000"
export INFRAHUB_API_TOKEN="06438eb2-8019-4776-878c-0941b1f1d1ec"
```

The token is the default admin token of a local Infrahub demo stack. Do not use it outside a local environment.

### 3. Start Infrahub and load the base data

```bash
uv run invoke setup
```

This starts the containers, loads the schemas, the menu and the bootstrap data. The UI is available at
<http://localhost:8000>.

The same steps can be run individually:

```bash
uv run invoke start          # start the Infrahub containers
uv run invoke load-schema    # schemas/base and schemas/extensions
uv run invoke load-menu      # navigation menu
uv run invoke load-objects   # data/bootstrap
```

### 4. Register the repository

```bash
uv run invoke register-repo --ref <branch>
```

This registers the repository with Infrahub (generators, transforms, checks and queries from `.infrahub.yml`) and
loads the event rules in `data/events/`. Without it, loading a scenario creates objects but no generator runs.

## Scenarios

Each scenario lives in [`data/demos/`](data/demos/) and has its own README with the architecture, the load steps
and what to verify.

### Data center fabrics

Each DC scenario loads one complete fabric. Generators build the pods, racks, devices, cabling, addressing and
routing from the design.

| Scenario | Location | Platform | DC design | Routing | Pods (design) | Naming | Demonstrates |
| -------- | -------- | -------- | --------- | ------- | ------------- | ------ | ------------ |
| [DC1](data/demos/01_data_center/dc1/) | Munich | Cisco Nexus 9K | `L` | `ebgp-ebgp` | 2 (`S_MIDDLE`) | standard | Reference 3-tier Clos: leaf, spine, super-spine, DC border-leafs with a firewall and load-balancer pair |
| [DC2](data/demos/01_data_center/dc2/) | Paris | Arista EOS | `M` | `ebgp-ebgp` | 4 (`S_MIDDLE`) | hierarchical | No super-spine tier (pod spines meshed back-to-back) while keeping a DC border-leaf tier |
| [DC3](data/demos/01_data_center/dc3/) | London | Cisco, Arista, Dell, Edgecore | `L` | `ebgp-ebgp` | 4 (`M_MIDDLE`) | flat | DC1 pattern at large scale, one vendor per pod under a Dell super-spine and border-leaf tier |
| [DC4](data/demos/01_data_center/dc4/) | Berlin | Arista EOS | `XL` | `ebgp-ebgp` | 3 (`S_MIDDLE`, `S_MIXED`, `S_TOR`) | hierarchical | Hyper-spine tier above the super-spines; all three pod deployment types in one DC |
| [DC5](data/demos/01_data_center/dc5/) | New York | Cisco, Arista, Dell, Edgecore | `L` | `ebgp-ebgp` | 4 (`S_MIDDLE`) | flat | Pod-level vendor diversity in one eBGP fabric with Cisco super-spines |
| [DC6](data/demos/01_data_center/dc6/) | Katowice | Cisco, Arista, Dell SONiC, Edgecore SONiC | `L` | `ebgp-ibgp` | 3 (`M_MIDDLE`, `S_TOR`, `M_MIXED`) | standard | Mixed vendors within pods, eBGP underlay with iBGP overlay (super-spines as route reflectors); base for the expansion scenarios |
| [DC7](data/demos/01_data_center/dc7/) | Amsterdam | Arista EOS | `S` | `ebgp-ebgp` | 2 (`S_BORDER_SPINE_POD`) | hierarchical | Micro-fabric: spine and border-leaf collapsed into a border-spine, no super-spine or DC border-leaf tier, a firewall and load-balancer pair per pod |

The designs, deployment types, port allocation and naming conventions are explained in
[IMPLEMENTATION.md](IMPLEMENTATION.md).

### Incremental changes

These scenarios change an existing fabric and show that the generators extend it without rebuilding it. The DC6
scenarios need DC6 loaded first.

| Scenario | Base | Change | Demonstrates |
| -------- | ---- | ------ | ------------ |
| [02_switch_dc6](data/demos/02_switch_dc6/) | DC6, Pod 1 | Rack `ktw-1-s-1-r-2-5` grows from 2 to 4 Dell L2 leafs | The rack generator re-runs on a `fabric_templates` change and adds only the new switches |
| [03_rack_dc6](data/demos/03_rack_dc6/) | DC6, Pod 1 | New network rack `ktw-1-s-1-r-4-5`: 2 leafs to the pod spines, 2 L2 leafs as a back-to-back MLAG | Adding one rack: cabling, MLAG and underlay ASN allocation |
| [04_pod_dc6](data/demos/04_pod_dc6/) | DC6 | New POD-4 (`M_MIDDLE`, Cisco NX-OS, vPC) in a new suite, with 2 network racks | The `add_pod` generator: new spines cabled to the existing super-spines, idempotent on re-run |
| [05_llm_time](data/demos/05_llm_time/) | DC1, Pod 1 | Spine count raised from 2 to 4 | Spine scale-out through a pod parameter change (described only; no data files) |

### Tenants, servers and applications (on DC6)

Load these in order on a branch where DC6 is already deployed.

| Scenario | Content | Demonstrates |
| -------- | ------- | ------------ |
| [06_servers](data/demos/06_servers/) | Server device type, compute racks and servers for DC6 pods 1 to 3, with bonded 25G interfaces | Endpoint cabling: servers are attached to the leaf or ToR pair of their row by the endpoint generator |
| [07_customers](data/demos/07_customers/) | Customers C001 (Nordix Ltd.), C002 (SwiftGo GmbH), C003 (Vaultex Inc.) and their DC6 footprints per environment | Customer boarding: footprint, service tier and customer address pools |
| [08_segments](data/demos/08_segments/) | VXLAN segments of the three customers, per environment and security zone | Segment generation: VNI, VLAN rows per switch, anycast gateway, VRF instances |
| [13_applications](data/demos/13_applications/) | Applications `storefront`, `checkout`, `dispatch-hub` and `custody-ledger`, their components, dependencies and a shared SASE egress proxy | Application-driven policy: firewall rules, ACLs and load-balancer VIPs derived from the declared dependencies |

### Colocation, cloud and SaaS

| Scenario | Content | Demonstrates |
| -------- | ------- | ------------ |
| [10_colocation](data/demos/10_colocation/) | Colocation points of presence at Equinix, Megaport and CoreSite in Europe and the United States | Colocation metros, zones and cage devices; loads on its own without data centers |
| [11_cloud](data/demos/11_cloud/) | AWS `eu-central-1`, Azure `westeurope` and GCP `europe-west4` with their availability zones | Cloud regions and zones as topology locations |
| [12_saas](data/demos/12_saas/) | Zscaler points of presence, a cloud security policy and the SaaS regions | SaaS security service modelled as topology, with explicit rule destinations |

### Complete topologies

| Scenario | Content | Demonstrates |
| -------- | ------- | ------------ |
| [00_full_topology](data/demos/00_full_topology/) | Hand-written sample: DCs Katowice, Warsaw and Gdansk, a Paris colocation cage, clouds, SaaS, an office with SD-WAN, and the internet | The complete data model end to end, with no generator involved; loaded by the fast integration test on its own branch |
| [30_all](data/demos/30_all/) | Three DC fabrics, colocation cages from three operators, three clouds, a SaaS edge, five offices, sixteen customers' footprints, nine applications and the interconnects between them | Every generator together on one branch; must be loaded stage by stage (`uv run invoke load-all-demo`) |

## Loading a Scenario

Load scenarios on a branch, never directly on `main`:

```bash
uv run infrahubctl branch create my-branch
uv run infrahubctl object load data/demos/01_data_center/dc6/ --branch my-branch
```

The generators start automatically when the loaded objects join their target groups (see
[`data/events/`](data/events/)). Follow progress in the Infrahub UI under Tasks; open a Proposed Change from the
branch to see the diff, the artifacts and the check results.

`30_all` references objects that generators create from earlier stages, so it has its own loader that waits for the
generators between stages:

```bash
uv run invoke load-all-demo                         # branch all-demo-scenario
uv run invoke load-all-demo --from-stage applications  # resume after a failed stage
```

## Testing

```bash
uv run invoke validate              # pre-commit hooks, smoke and unit tests with coverage
uv run invoke test-unit             # unit tests only
uv run invoke test-integration-fast # setup, repository sync and the 00_full_topology sample (Docker)
```

Integration profiles, the choice of profile for a given change, and macOS notes are described in
[CONTRIBUTING.md](CONTRIBUTING.md#testing) and [tests/integration/README.md](tests/integration/README.md).

## Troubleshooting

- **Port conflict:** make sure no other service uses port 8000 (the test stack uses 8100).
- **Dependency errors:** run `uv sync --group dev` again.
- **Containers do not start:** check that Docker is running and that your user may access it.
- **No generator runs after a load:** the repository is not registered, or the event rules are not loaded; run
  `uv run invoke register-repo`.
- **macOS with Colima:** Colima does not share the system temporary folder (`/var/folders/...`) with the VM, so
  integration stacks started from a pytest temporary directory fail with `Failed to start docker compose`. The invoke
  test tasks already use a repository-local `--basetemp`. Alternatively, start Colima with the folder mounted:

  ```bash
  colima stop
  colima start --mount type=virtiofs,source=/var/folders,destination=/var/folders
  ```

## Further Documentation

| Document | Content |
| -------- | ------- |
| [CONTRIBUTING.md](CONTRIBUTING.md) | Development workflow, quality checks, tests, commits and releases |
| [IMPLEMENTATION.md](IMPLEMENTATION.md) | Fabric designs, pod deployment types, port allocation, naming and cabling |
| [AGENTS.md](AGENTS.md) | Data model rules and conventions in full (also the guidance for AI coding assistants) |
| [tests/integration/README.md](tests/integration/README.md) | Integration test suite |
| [CHANGELOG.md](CHANGELOG.md) | Release history |
| [Infrahub documentation](https://docs.infrahub.app) | Infrahub and its Python SDK |

## License

Licensed under the Apache License 2.0. See [LICENSE](LICENSE).
