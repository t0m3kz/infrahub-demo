# 30_all: The Everything Demo

Every other demo shows one piece. This one loads all of them into one branch: three DC fabrics, colocation
cages from three operators, three clouds, a SaaS edge, four offices, sixteen customers' footprints, seven
applications, and the interconnect layer that ties them together.

It is also the only demo that **must be loaded in order**. The steps below are that order.

---

## Why It Has to Be Staged

Several files point at objects that no file declares. Generators create those objects, and they run
asynchronously after the load that triggers them:

- `03_dc/*/05_servers.yml` puts hosts in compute racks. `add_endpoint` then looks for the access-leaf pair
  in the same row, and those leafs only exist once `add_rack` has run.
- `08_interconnects/01_colo_onramp/02_interfaces.yml` references border leafs by name (`bl-dc101101`).
  `add_dc` creates them.
- `07_applications` references VMs and customer footprints declared in `06_customer_boarding`.

So `infrahubctl object load data/demos/30_all` in one go only works on an instance whose branch already has
the fabrics. That is why it seems to work on a long-lived dev instance and fails on a fresh one. **Load one
stage, wait for its generators to finish, then load the next.**

---

## Prerequisites

Start Infrahub and load the schema, menu and bootstrap data:

```bash
export INFRAHUB_ADDRESS="http://localhost:8000"
export INFRAHUB_API_TOKEN="06438eb2-8019-4776-878c-0941b1f1d1ec"

uv run invoke setup
```

Register the repository and load the event actions. Without these, no generator fires and the later stages
fail with missing references:

```bash
uv run invoke register-repo --ref main
```

`--ref` is the Git branch Infrahub imports generators, transforms and checks from. Point it at the branch
whose code you want to run.

---

## Loading the Demo

Demo data never goes into `main`. One task creates the `all-demo-scenario` branch and loads every stage in
order, waiting for the generators after each one:

```bash
uv run invoke load-all-demo
```

It stops at the first stage whose load fails or whose generators fail, and prints the command to resume.
Fix the cause, then pick up where it stopped:

```bash
uv run invoke load-all-demo --from-stage applications
```

`--branch` loads into another branch instead. Stage 3 alone takes a while, so expect the whole run to take
some time.

### Loading by Hand

The task runs the steps below. Use them if you want to watch or change a single stage. Create a branch first:

```bash
uv run infrahubctl branch create all-demo-scenario
export BRANCH=all-demo-scenario
```

Load the stages **in this order**. After each one, wait until no generator task is still running (see
[Waiting Between Stages](#waiting-between-stages)).

| # | Stage | What it loads | Generators it fires |
| --- | --- | --- | --- |
| 1 | foundation | Customers, cloud regions/zones, colocation metros/cages/racks, SaaS, office buildings | `add_colocation_metro` (×9) |
| 2 | dc_locations | DC10/DC11/DC12 campus and suite tree | — |
| 3 | dc_fabric | The three fabrics and their controllers | `add_dc` → `add_pod` → `add_rack` (longest stage) |
| 4 | dc_compute | Application hosts in the compute racks | `add_endpoint` |
| 5 | dc_customers | DC footprints of DC-only customers | `add_customer_deployment_dc` |
| 6 | customer_boarding | DC/colocation/cloud/office footprints, cage kit, VMs | `add_customer_deployment_*`, `add_sdwan_edge` |
| 7 | applications | Segments, applications, deployment-request catalogue | `add_vxlan_segment`, `add_app_application` |
| 8 | interconnects | Colo on-ramp, cloud hub, virtual circuits, SD-WAN, cloud endpoints | — (data only) |

```bash
D=data/demos/30_all

# 1. foundation
uv run infrahubctl object load $D/00_customer $D/01_cloud $D/02_colo $D/04_saas $D/05_office --branch $BRANCH

# 2. dc_locations
uv run infrahubctl object load $D/03_dc/dc10/00_location.yml $D/03_dc/dc11/00_location.yml \
  $D/03_dc/dc12/00_location.yml --branch $BRANCH

# 3. dc_fabric
uv run infrahubctl object load $D/03_dc/dc10/01_topology.yml \
  $D/03_dc/dc11/01_controllers_virtual.yml $D/03_dc/dc11/02_topology.yml \
  $D/03_dc/dc12/01_controllers_physical.yml $D/03_dc/dc12/02_controllers_virtual.yml \
  $D/03_dc/dc12/03_topology.yml --branch $BRANCH

# 4. dc_compute
uv run infrahubctl object load $D/03_dc/dc10/05_servers.yml $D/03_dc/dc11/05_servers.yml \
  $D/03_dc/dc12/05_servers.yml --branch $BRANCH

# 5. dc_customers
uv run infrahubctl object load $D/03_dc/new_customers --branch $BRANCH

# 6. customer_boarding
uv run infrahubctl object load $D/06_customer_boarding --branch $BRANCH

# 7. applications
uv run infrahubctl object load $D/07_applications --branch $BRANCH

# 8. interconnects
uv run infrahubctl object load $D/08_interconnects --branch $BRANCH
```

Load each stage with a single `object load` call. The loader already sorts files and loads them one after
another. Splitting a stage across several calls does not make it faster, and it can hide one file's writes
from the next.

### Waiting Between Stages

A stage is done when no task is pending or running anymore:

```bash
uv run infrahubctl task list --state SCHEDULED --state PENDING --state RUNNING
```

Run it until the list is empty, then check that nothing failed:

```bash
uv run infrahubctl task list --state FAILED --state CRASHED
```

You can also watch the **Tasks** page in the UI. Fix any failed task before you load the next stage: a
failure in an early stage turns into dozens of misleading reference errors later on. Stage 3 takes by far
the longest, because it generates around 20 devices per DC plus all their cabling, addressing and routing.

---

## Checking the Result

Once stage 8 has settled, the branch should contain at least:

| Kind | Count |
| --- | --- |
| `TopologyDataCenter` | 3 |
| `TopologyPod` | 6 |
| `LocationRack` | 25 |
| `TopologyCustomerDC` | 7 |
| `TopologyCustomerColocation` | 7 |
| `TopologyCustomerCloud` | 4 |
| `TopologyCustomerOffice` | 5 |
| `ManagedVxlanSegment` | 5 |
| `AppApplication` | 7 |

Generators add more devices, prefixes and segments on top of what the files declare, so read these as
minimums. To review the whole thing as a change, open a Proposed Change from `all-demo-scenario` into `main`.
Infrahub then runs the checks and generates the device configuration artifacts.

---

## Testing the Load

The integration suite loads the same stages on a throwaway Infrahub (testcontainers). It then also
checks that every expected generator ran, and verifies the application graph, the compute layer, the
interconnects and the change-risk check:

```bash
uv run invoke test-integration-all-demo
```

`invoke load-all-demo` and the test suite both read the stage list from `ALL_DEMO_LOAD_STAGES` in
[tasks.py](../../../tasks.py). If you add a file to this demo that depends on generated objects, add it to
the matching stage there and in the table above.
