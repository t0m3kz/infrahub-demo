# Infrahub demo

[![Ruff][ruff-badge]][ruff-link]
[![Python][python-badge]][python-link]
[![Actions status][github-badge]][github-link]
[![Latest release][release-badge]][release-link]
[![License][license-badge]][license-link]
[![Contributors][contributors-badge]][contributors-link]
[![Coverage Status][coverage-badge]][coverage-link]
[![GitHub Discussion][github-discussions-badge]][github-discussions-link]

---

## Why Infrahub Demo?

> "The machine cannot be blamed. It is doing precisely what it was instructed to do.
> The real culprits are those who sit idly by, awaiting some miraculous vendor contraption
> that will magically satisfy all their peculiar requirements—instead of rolling up their sleeves
> and constructing it themselves."
>
> — *Inspired by Stanisław Lem's "Memoirs Found in a Bathtub"*

**My Journey:**
I was exhausted. Exhausted by vendor tools that promised the moon but delivered a handful of dust.

Exhausted by being herded into their GUI labyrinths—click here, click there, click everywhere—imprisoned in their rigid schemas, held hostage by their glacial update cycles.

These so-called "solutions" arrived festooned with monitoring dashboards and deployment wizards that turned your controller into a wheezing, resource-gobbling behemoth requiring constant massage and sweet-talking just to stay upright.

And don't even get me started on YAML files so deeply nested and interdependent that changing one variable requires a PhD in archaeology to trace what will explode three modules away.

Infrastructure as Code was supposed to mean freedom—not just shuffling YAML incantations into some vendor's inscrutable black box and praying to the Cloud Gods for mercy.

I wanted *absolute sovereignty over my data model*, the liberty to forge my own relationships, the power to orchestrate everything through code—no "Wizard Step 3 of 47" required, no integration hell spanning multiple vendor portals where you need a Rosetta Stone just to make two systems talk to each other.

And I was especially done with external vendor developers who treat automation like a YOLO sport—deploying untested code straight into production, using customer company as their personal test lab, and only realizing what went wrong when the alarms go off. If you dare to question their approach, they escalate to the highest level, as if basic testing is an outrageous demand.

For those blissfully unaware, "YOLO" means "You Only Live Once"—in this context, it’s developer-speak for "deploy now, worry never," and a total disregard for actual results.

Infrahub changed that. It's not another tool forcing strict data definitions or telling me how to think about infrastructure. Instead, it lets me define my own data structures—a new approach that puts me in control of how my infrastructure is modeled and managed. Design-driven automation means I own the schemas, I own the logic, I own the destiny of my network. No more vendor lock-in, no more GUI dependency, no more compromise. And best of all, it delivers an easy-to-use approach for unit testing and integration testing—so you can actually trust your automation before it hits production.

This demo is trying prove it: from topology design to device generation, from configuration templates to validation checks—all driven by my data structures, all automated through my code. That's the infrastructure revolution I was waiting for.

But topology is just the appetizer. The main course—the part that makes seasoned network engineers put down their coffee and stare into the middle distance—is turning the whole request process inside out. No more filing a ticket, waiting three sprints, and getting back a firewall rule that's subtly wrong. Instead, an application team declares *what their application actually needs*: "checkout-service talks to payment-gateway on 443, full stop." That single declaration is the source of truth, and Infrahub's generators do the tedious, error-prone plumbing on your behalf—the firewall rule, the proxy config, the security policy, the routing, all derived from the same intent, all consistent with each other, all versioned in a branch you can review before it touches anything real.

This is application-driven infrastructure: the network exists to serve the application, not the other way around. No more "just open a change request and we'll get to it eventually." No more hand-carved ACLs that nobody dares touch because nobody remembers why they're there. You declare intent, the generators do the honest work, and the humans get their Friday afternoons back.

**A word about ServiceNow.** Nothing against it: somebody has to own the ticket number, the change advisory board
calendar and the approvals the auditors accept. But a change request typed by hand is a work of fiction written at
4:55pm on a Friday, and the diff is the only part of it that was ever true. So this demo derives the request from the
diff (devices, risk, rollout, backout, test plan, a maintenance window), gates the merge on it, and renders it as a
ServiceNow-shaped body you can hand over (`change_request_servicenow`). ServiceNow stays the system of record; the demo
is merely the part that does the typing. See [Change Requests](#change-requests).

**Special thanks to [OpsMill](https://opsmill.com) for making this happen** – they built Infrahub with the vision that infrastructure teams should have complete control, not be prisoners to vendor constraints. (And yes, I'm bloody jealous I didn't have the power and motivation to come up with such a brilliant idea myself – like Prometheus watching others steal the fire of the gods!)

**To companies/vendors** who may borrow ideas from this repo for your customer environments: please sponsor at least 3 open source communities. Odds are, open source powers half your commercial products anyway. The volunteers who build these tools deserve more than just a "thanks" and a GitHub star. And if you’ve ever filed a bug report and then complained about how long it takes to fix, remember: a few dollars a month for open source won’t even dent your coffee budget—so go ahead, give back and help keep these communities caffeinated and strong.

**In short:** this repository models data center, colocation, cloud, SaaS and office networks in
[Infrahub](https://docs.infrahub.app) and generates them from design data. Schemas define the data model, generators
build the topology, transforms render device configurations, and checks validate the result before a change is merged.
Two ideas drive it:

- **Design-driven topology.** A fabric is described by its design (size, pod layout, routing strategy, naming
  convention). Generators derive the devices, cabling, addressing and routing from it, so a new pod, rack or switch
  is a data change, not a manual build.
- **Application-driven infrastructure.** An application team declares what its application needs (for example
  "checkout talks to payment on tcp/443"). Generators derive the firewall rules, proxy configuration, security policy
  and routing from that one declaration, consistently, on a branch that is reviewed before it is merged.

*On the design-driven approach:* a DC6 fabric of forty-odd devices, with their cabling, ASNs and addresses, comes from a
handful of YAML files and one command, and nobody typed a single cable into a spreadsheet. The spreadsheet is the same
age as the network it describes, and about as accurate. Here the design is the source of truth and the devices are an
output, which is a rare case of the documentation being unable to lie.

*On the application-driven approach:* the firewall rule exists because an application declared what it needs, not
because somebody remembered to ask in the right queue. The security team keeps its job, and gets a better one: reviewing
a diff of what will actually change, instead of transcribing a ticket into five vendors' syntax and hoping the ticket
was right.

---

## Requirements

- Python 3.11, 3.12, 3.13, or 3.14
- [uv](https://github.com/astral-sh/uv) for dependency management
- Docker (the Infrahub stack, containerlab and the integration tests)
- **Infrahub 1.10** or higher (the registered flattened queries need **1.12**; the integration stack runs **1.12.1**)

> **Version Compatibility Note**: the data models and generators can be modified to work with older versions of
> Infrahub if needed, but the registered queries are flattened copies that need 1.12 (see [AGENTS.md](AGENTS.md)).
> Who does the modifying is traditionally settled by whoever missed the upgrade planning meeting.

## Features

- Design-driven network automation demo using [Infrahub](https://docs.infrahub.app)
- Application-driven infrastructure: declare app dependencies once ("app A talks to app B on 443") and let generators materialize the firewall rules, proxy config, security policy, and routing—no three-sprint change request required
- Kicks: every demo deploys as a series of reviewed changes with one command (`uv run invoke kick dc6`), each stage a branch, a proposed change, its checks and artifacts, and a merge
- Change requests: a Proposed Change gets a ServiceNow-style change request whose description, risk, rollout,
  backout and test plans are derived from its own diff, created by Infrahub itself and gated by a check
  ([docs/change_requests.md](docs/change_requests.md))
- Example data, schemas, and menu for rapid onboarding
- Invoke tasks for bootstrapping, deploying demos, and CI integration
- Modular structure for easy extension and experimentation

## Project Structure

- [`checks/`](checks/) – Custom validation logic
- [`data/`](data/) – Example data for bootstrapping
- [`generators/`](generators/) – Topology and config generators
- [`menu/`](menu/) – Example menu definition
- [`queries/`](queries/) – GraphQL queries for Infrahub
- [`schemas/`](schemas/) – Base and extension schemas
- [`tasks/`](tasks/) – Invoke tasks and the kicks (`kicks.yml`) that deploy the demos
- [`templates/`](templates/) – Jinja2 templates for device configs
- [`tests/`](tests/) – Unit, integration, and smoke tests
- [`transforms/`](transforms/) – Python transforms for Infrahub
- [`utils/`](utils/) – Commonly used helpers

## Quickstart

### Use GitHub Codespaces

Deploy demo using Codespaces:
[![Launch in GitHub Codespaces](https://img.shields.io/badge/Launch%20Infrahub%20Demo-0B6581?logo=github)](https://codespaces.new/t0m3kz/infrahub-demo?devcontainer_path=.devcontainer%2Fdevcontainer.json&ref=stable)

### Setup environment variables

```bash
export INFRAHUB_ADDRESS="http://localhost:8000"
export INFRAHUB_API_TOKEN="06438eb2-8019-4776-878c-0941b1f1d1ec"
```

### Install the Infrahub SDK

```bash
uv sync
```

### Quick Setup (Recommended)

One command to setup everything:

```bash
uv run invoke setup
```

This starts the dev stack if it is not running, loads schemas, menu and bootstrap data, and registers the repository
with its event rules. Then explore at <http://localhost:8000>

### Manual Setup (Alternative)

Load schemas

```bash
uv run invoke load-schema
```

Load menu

```bash
uv run invoke load-menu
```

Load demo data

```bash
uv run invoke load-objects
```

Register repository and load event actions (`setup` already does this when no repository is registered)

```bash
uv run invoke register-repo
```

### Deploy Data Center Scenarios

**6 strategic scenarios** demonstrating vendor platforms, naming strategies, multi-vendor architectures.
Each deploys with one command, `uv run invoke kick dc1` (`dc2` ... `dc7`), see [Deploying a Scenario](#deploying-a-scenario).

#### Single-Vendor Deployments (DC1-DC4)

| Scenario | Location | Vendor | Deployment | Description |
| ---------- | ---------- | -------- | ------------ | ------------- |
| **[DC1](data/demos/01_data_center/dc1/)** | Munich 🇩🇪 | Cisco | All (MR+Mixed+ToR) | Hierarchy Overkill: All the racks, all the drama. |
| **[DC2](data/demos/01_data_center/dc2/)** | Paris 🇫🇷 | Arista | Middle Rack | Croissants & Cheap Packets: Small, efficient, and CFO-approved. |
| **[DC3](data/demos/01_data_center/dc3/)** | London 🇬🇧 | Dell/SONiC | Flat ToR | Brexit, No Middle Management, Maximum Sass. |
| **[DC4](data/demos/01_data_center/dc4/)** | Berlin 🇩🇪 | Edgecore/SONiC | Mixed + ToR | Maximum Chaos, Minimum Rules. |

#### Multi-Vendor Deployments (DC5-DC6)

| Scenario | Location | Architecture | Description |
| ---------- | ---------- | ------------- | ------------- |
| **[DC5](data/demos/01_data_center/dc5/)** | New York 🇺🇸 | Different vendor per pod | Eurovision for Switches: 4 pods, 4 vendors, 0 peace. |
| **[DC6](data/demos/01_data_center/dc6/)** | Katowice 🇵🇱 | Mixed vendors within pods | Silesian Buffet, Vendor Bingo, Debug & Dine. |

#### Expansion & Incremental Deployment Scenarios

Explore LLM upgrades and organic growth patterns—all in one place. The switch, rack, pod and spine demos grow DC6, so
make sure DC6 is already deployed (`uv run invoke kick dc6-growth` does the whole path); llm time works on DC1:

| Scenario | Location | Type/Architecture | Description |
| ---------- | ---------- | ------------------- | ------------- |
| **[switch](data/demos/02_switch_dc6/)** | Katowice 🇵🇱 | Rack Expansion | "Just TWO more switches"—organic chaos. |
| **[rack](data/demos/03_rack_dc6/)** | Katowice 🇵🇱 | Leaf + Access-Leaf Rack | Minimalist rack: started as a test, now it's critical. |
| **[pod](data/demos/04_pod_dc6/)** | Katowice 🇵🇱 | Pod Expansion | Pod 4: because 3 wasn't enough. |
| **[spine](data/demos/05_spine_dc6/)** | Katowice 🇵🇱 | Spine Expansion | One more spine in POD-1: a data change, not a purchase order. |
| **[llm time](data/demos/09_llm_time/)** | Munich 🇩🇪 | Spine Expansion | Extra spines for LLMs—plausible deniability included (illustrative only, no kick loads it). |

#### Everything at Once

**[30_all](data/demos/30_all/)** loads every fabric, colocation, cloud, office, customer and application into
one branch. It has to be loaded stage by stage, so use `uv run invoke kick all-demo` (or `load-all-demo`, the same
kick), which loads the stages in order and waits for the generators in between. The demo README explains why and how
to do it by hand.

Brace yourself: even more questionable use cases, wild topologies, and vendor drama are coming soon. If you want to see even more chaos, star this repo—so the author can unlock extra GitHub tools and automate his caffeine intake. Your star may be the difference between a new feature and another debugging session at midnight!

## Deploying a Scenario

Demos are deployed by **kicks**: YAML in [`tasks/kicks.yml`](tasks/kicks.yml), run with `invoke`.

```bash
uv run invoke kicks                                     # list every kick, demo and suite
uv run invoke kick dc6                                  # deploy DC6 as a change
uv run invoke kick all-demo --from-stage applications   # resume after a failed stage
```

A kick is a list of stages. A kick with a `branch` loads its stages onto that branch in turn (`all-demo` loads
`30_all` onto `all-demo-scenario`; `uv run invoke load-all-demo` is the same kick). A kick without one makes each stage
a **change**: branch `<kick>-<stage>` from `main`, load, wait for the generators to settle, a proposed change, its
checks and artifacts, then the merge. Failed tasks must be zero throughout.

`invoke kick` looks at the stack first. It waits for a repository that is still syncing, runs `invoke setup` when the
bootstrap data, the repository or a generator is missing, and starts a dev stack only at <http://localhost:8000>.
An `INFRAHUB_ADDRESS` that answers nothing and is not that one is refused.

To load a folder without a kick (never directly on `main`):

```bash
uv run infrahubctl branch create my-branch
uv run infrahubctl object load data/demos/01_data_center/dc6/ --branch my-branch
```

The generators start automatically when the loaded objects join their target groups (see
[`data/events/`](data/events/)). Follow progress in the Infrahub UI under Tasks; open a Proposed Change from the
branch to see the diff, the artifacts and the check results. `30_all` references objects that generators create in
earlier stages, which is why it only loads through its kick.

## Change Requests

A Proposed Change gets a ServiceNow-style [change request](docs/change_requests.md), and nobody has to ask for it.
When the last artifact validator of the PC completes, Infrahub creates `CHG-<pc>` on the source branch (or re-derives
it), and the generator `add_change_request` fills it in from the PC's own diff and artifacts: devices, customers,
risk, change type, plans and a maintenance window. It also writes a "Change request" thread on the PC (link, type, risk,
devices, window, ticket), because the UI shows a PC only through its fixed pages. The check
`validate_change_request` gates the merge of a PC that has one. No outside service is needed. The change advisory board
still meets, but most of its paperwork now writes itself from the diff instead of from someone's optimism at 4:55pm on
a Friday.

Who is more flexible, ServiceNow or this demo?

| | ServiceNow | This demo |
| --- | --- | --- |
| Content | Typed by the requester, once | Derived from the diff, re-derived after every pipeline run |
| Window | Negotiated | Computed: next Thursday 22:00 Europe/Warsaw, 10 minutes per device, at least 30 |
| Risk | A questionnaire | Scored from what actually changes (roles, removals, routing, customers) |
| Merge | Waits for an approval | Refused by `validate_change_request` when the request is incomplete |
| A new rule | A workflow designer and a consultant | A few lines of Python and a template |
| Ticket number, CAB workflow, approvals with teeth, an audit trail auditors accept | Yes | None. `external_id` stays empty until someone files it |

The demo renders a ServiceNow-shaped body on demand (`change_request_servicenow`), so it can feed the real thing. It
is flexible about everything except being accountable.

## CI/CD

This project uses GitHub Actions for continuous integration. All pushes and pull requests are tested for lint, type checks, and unit tests.
A robot notices a broken commit long before a customer does, and it never asks for a meeting about it.

## Security & Secrets

- Do not commit real API tokens. Use `.env` or GitHub secrets for sensitive data in production.
- Example tokens in this README are for demo purposes only (they open a local demo stack, not the procurement vault).

## Troubleshooting

- If you encounter port conflicts, ensure no other service is running on port 8000
  (usually a stack you forgot you started last Tuesday).
- For dependency issues, run `uv sync` again.
- For Docker/infrahub issues, ensure Docker is running and you have the correct permissions.
- **macOS + Colima + integration tests**: Colima often does not share the system temp folder (e.g. `/var/folders/...`) into the VM.
Some integration tests start Docker Compose stacks from a pytest temp directory, and missing file sharing can break bind mounts (notably `haproxy.cfg`), causing the stack to fail with `Failed to start docker compose`.
Use a repo-local pytest temp directory via `--basetemp` (see Testing below).

## Testing

Run all tests using (before the Friday deploy, not after it):

```bash
uv run inv validate
```

Or run specific test scripts in the [`tests/`](tests/) directory.

### macOS + Colima

If you run Docker via Colima, prefer a repo-local pytest temp directory so Docker bind mounts always come from a shared path:

```bash
uv run invoke test-unit
uv run invoke test-integration
```

These tasks run pytest with a repo-local basetemp.

### Integration Test Profiles

The integration suite shares one Docker stack per pytest invocation, which is much cheaper than starting a fresh small
universe for every file. It is three files: `test_01` (schemas and bootstrap), `test_02` (repository sync) and `test_04`
(the suite kicks of [`tests/integration/kicks/`](tests/integration/kicks/): `dc6-suite` grows DC6 change by change,
`full-topology-suite` loads the full topology as one checked change), with assertions per stage.

```bash
# Setup, repository sync and the full-topology sample on its own branch
uv run invoke test-integration-fast

# Everything, including dc6-suite (about half an hour)
uv run invoke test-integration
```

The test stack serves on port **8100**, so it can run next to your dev stack on 8000. A stage fails on the first failed
task, and the failure names it. Which profile a change needs, and what the suite no longer covers, is in
[CONTRIBUTING.md](CONTRIBUTING.md#testing) and [tests/integration/README.md](tests/integration/README.md).

Alternative (manual):

```bash
INFRAHUB_TESTING_ENABLE_INTEGRATION=1 uv run pytest -vv tests/integration --basetemp .pytest-tmp
```

If you want a VM-wide fix instead, start Colima with `/var/folders` mounted (heavier/less portable):

```bash
colima stop
colima start --mount type=virtiofs,source=/var/folders,destination=/var/folders
```

## 🙏 A Note from a Highly Fallible Carbon-Based Life Form

> Look, I'm just a human who occasionally writes code between coffee breaks and existential crises.
> If something explodes, catches fire, or simply refuses to work as advertised — like that one switch
> that's been "temporarily" in your rack since 2019 — please
> [open an issue](https://github.com/t0m3kz/infrahub-demo/issues).
> I promise to investigate with the same determination I use to debug production on a Friday afternoon.
> Your feedback is invaluable, unlike my initial variable naming choices!

## ⚠️ A Word About the Jokes

All sarcastic remarks, vendor opinions, city stereotypes, procurement committee jokes,
AI commentary, and observations about cloud billing scattered throughout this project
and its demo READMEs are drawn entirely from personal experience surviving real enterprise
infrastructure projects over many years.

They are meant to be funny. They are absolutely not meant to insult anyone —
vendors, cities, ISPs, hypervisor platforms, cloud providers, users, managers,
consultants, or the three people who still believe the monolithic controller was a good idea.

If you recognise your organisation, your network, your Friday-afternoon architecture decision,
or your specific brand of procurement committee in any of this: welcome.
You are among friends. We have all been there.

Enterprise infrastructure is a universal human experience that transcends company, country,
and technology stack. The chaos is the same everywhere. The YAML just has different names.

> *"Any sufficiently complex infrastructure is indistinguishable from something a junior engineer
> built at 2am before a product launch. The difference is only detectable in the git log."*
> — Not a real quote, but truer than most real ones

## Contributing

Contributions, questions, and feedback are welcome! Please use [GitHub Discussions][github-discussions-link].

## References

- [Infrahub Documentation](https://docs.infrahub.app)
- [Project Discussions](https://github.com/t0m3kz/infrahub-demo/discussions/)

## License

This project is licensed under the MIT License.

[ruff-badge]:
<https://img.shields.io/endpoint?url=https://raw.githubusercontent.com/astral-sh/ruff/main/assets/badge/v2.json>
[ruff-link]:
<https://github.com/astral-sh/ruff>
[github-discussions-link]:
<https://github.com/t0m3kz/infrahub-demo/discussions/>
[github-discussions-badge]:
<https://img.shields.io/static/v1?label=Discussions&message=Ask&color=blue&logo=github>
[github-badge]:
<https://github.com/t0m3kz/infrahub-demo/actions/workflows/main.yml/badge.svg>
[github-link]:
<https://github.com/t0m3kz/infrahub-demo/actions/workflows/main.yml>
[coverage-badge]:
<https://img.shields.io/codecov/c/github/t0m3kz/infrahub-demo?label=coverage>
[coverage-link]:
<https://codecov.io/gh/t0m3kz/infrahub-demo>
[python-badge]:
<https://img.shields.io/badge/python-3.11%7C3.12%7C3.13%7C3.14-000000?logo=python>
[python-link]:
<https://www.python.org>
[release-badge]:
<https://img.shields.io/github/v/release/t0m3kz/infrahub-demo>
[release-link]:
<https://github.com/t0m3kz/infrahub-demo/releases/latest>
[license-badge]:
<https://img.shields.io/github/license/t0m3kz/infrahub-demo>
[license-link]:
<https://github.com/t0m3kz/infrahub-demo/blob/main/LICENSE>
[contributors-badge]:
<https://img.shields.io/github/contributors/t0m3kz/infrahub-demo>
[contributors-link]:
<https://github.com/t0m3kz/infrahub-demo/graphs/contributors>
