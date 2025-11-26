# Infrahub demo

[![Ruff][ruff-badge]][ruff-link]
[![Python][python-badge]][python-link]
[![Actions status][github-badge]][github-link]
[![Coverage Status][coverage-badge]][coverage-link]
[![GitHub Discussion][github-discussions-badge]][github-discussions-link]

---
## Why Infrahub Demo?

> "The machine cannot be blamed. It is doing precisely what it was instructed to do. The real culprits are those who sit idly by, awaiting some miraculous vendor contraption that will magically satisfy all their peculiar requirements—instead of rolling up their sleeves and constructing it themselves."
>
> — *Inspired by Stanisław Lem's "Memoirs Found in a Bathtub"*

**My Journey:**
I was exhausted. Exhausted by vendor tools that promised the moon but delivered a handful of dust.

Exhausted by being herded into their GUI labyrinths—click here, click there, click everywhere—imprisoned in their rigid schemas, held hostage by their glacial update cycles.

These so-called "solutions" arrived festooned with monitoring dashboards and deployment wizards that turned your controller into a wheezing, resource-gobbling behemoth requiring constant massage and sweet-talking just to stay upright.

And don't even get me started on YAML files or Terraform configurations so deeply nested and interdependent that changing one variable requires a PhD in archaeology to trace what will explode three modules away.

Infrastructure as Code was supposed to mean **freedom**—not just shuffling YAML incantations into some vendor's inscrutable black box and praying to the Cloud Gods for mercy.

I wanted **absolute sovereignty over my data model**, the liberty to forge my own relationships, the power to orchestrate everything through code—no "Wizard Step 3 of 47" required, no integration hell spanning multiple vendor portals where you need a Rosetta Stone just to make two systems talk to each other.

And I was especially done with external vendor developers who treat automation like a YOLO sport—deploying untested code straight into production, using customer company as their personal test lab, and only realizing what went wrong when the alarms go off. If you dare to question their approach, they escalate to the highest level, as if basic testing is an outrageous demand.

For those blissfully unaware, "YOLO" means "You Only Live Once"—in this context, it’s developer-speak for "deploy now, worry never," and a total disregard for actual results.

Infrahub changed that. It's not another tool forcing strict data definitions or telling me how to think about infrastructure. Instead, it lets me define my own data structures—a new approach that puts me in control of how my infrastructure is modeled and managed. Design-driven automation means I own the schemas, I own the logic, I own the destiny of my network. No more vendor lock-in, no more GUI dependency, no more compromise.

This demo is trying prove it: from topology design to device generation, from configuration templates to validation checks—all driven by **my data structures**, all automated through **my code**. That's the infrastructure revolution I was waiting for.

**Special thanks to [OpsMill](https://opsmill.com) for making this happen** – they built Infrahub with the vision that infrastructure teams should have complete control, not be prisoners to vendor constraints. (And yes, I'm bloody jealous I didn't have the power and motivation to come up with such a brilliant idea myself – like Prometheus watching others steal the fire of the gods!)

**To companies/vendors** who may be inspired by ideas from this repository and use them in their customer environments: please consider sponsoring 3 open source communities. It's crucial to ensure that the incredible volunteers who build these tools don't feel like losers. They deserve recognition and support for their outstanding contributions to the tech community. I sincerely hope you've earned enough that a few dollars a month sponsoring open source communities won't cause financial collapse – you can well afford to give back.

---

## Requirements
- Python 3.10, 3.11, or 3.12
- [uv](https://github.com/astral-sh/uv) for dependency management
- Docker (for containerlab and some integration tests)
- **Infrahub 1.5** (currently in beta) - See note below

> **Version Compatibility Note**: This demo is designed for Infrahub 1.5 (beta). The data models and generators can be modified to work with previous versions of Infrahub if needed.

## Features
- Design-driven network automation demo using [Infrahub](https://docs.infrahub.app)
- Example data, schemas, and menu for rapid onboarding
- Scripts for bootstrapping, demo use cases, and CI integration
- Modular structure for easy extension and experimentation

## Project Structure
- [`checks/`](checks/) – Custom validation logic
- [`data/`](data/) – Example data for bootstrapping
- [`generators/`](generators/) – Topology and config generators
- [`menu/`](menu/) – Example menu definition
- [`queries/`](queries/) – GraphQL queries for Infrahub
- [`schemas/`](schemas/) – Base and extension schemas
- [`scripts/`](scripts/) – Helper scripts for automation
- [`templates/`](templates/) – Jinja2 templates for device configs
- [`tests/`](tests/) – Unit, integration, and smoke tests
- [`transforms/`](transforms/) – Python transforms for Infrahub

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

This loads schemas, bootstrap data, and menu. Then explore at http://localhost:8000

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

### Deploy Data Center Scenarios

**6 strategic scenarios** demonstrating vendor platforms, naming strategies, multi-vendor architectures.

#### Single-Vendor Deployments (DC1-DC4)
| Scenario | Location | Vendor | Deployment | Description |
|----------|----------|--------|------------|-------------|
| **[DC1](data/demos/01_data_center/dc1/)** | Munich 🇩🇪 | Cisco | All (MR+Mixed+ToR) | The Kitchen Sink - 28 racks showcasing ALL deployment types because why choose when you can have it all? |
| **[DC2](data/demos/01_data_center/dc2/)** | Paris 🇫🇷 | Arista | Middle Rack | Croissant-powered EOS efficiency - proving you don't need a mortgage to build reliable infrastructure |
| **[DC3](data/demos/01_data_center/dc3/)** | London 🇬🇧 | Dell/SONiC | Flat ToR | Brexit-proof flat topology - more fiber under the Thames than extra hops in your packet path |
| **[DC4](data/demos/01_data_center/dc4/)** | Berlin 🇩🇪 | Edgecore/SONiC | Mixed + ToR | Hipster open networking - as edgy as the techno scene, vendor-neutral like Berlin's nightlife |

#### Multi-Vendor Deployments (DC5-DC6)
| Scenario | Location | Architecture | Description |
|----------|----------|-------------|-------------|
| **[DC5](data/demos/01_data_center/dc5/)** | New York 🇺🇸 | Different vendor per pod | Wall Street's playground - 4 pods, 4 vendors, cheaper than a Manhattan studio (the racks, not the servers) |
| **[DC6](data/demos/01_data_center/dc6/)** | Katowice 🇵🇱 | Mixed vendors within pods | Polish efficiency meets vendor chaos - half Western European cost, full interoperability headaches |

#### Expansion & Incremental Deployment Scenarios

Explore LLM upgrades and organic growth patterns—all in one place:

| Scenario | Location | Type/Architecture | Description |
|----------|----------|-------------------|-------------|
| **[02_switch](data/demos/02_switch/)** | Munich 🇩🇪 | Rack Expansion | "Just TWO more switches" - 2 ToRs to existing rack. How organic growth happens. |
| **[03_rack](data/demos/03_rack/)** | Munich 🇩🇪 | Minimal ToR | The minimalist approach - single ToR rack that started as a "test" and became critical infrastructure. |
| **[04_pod](data/demos/04_pod/)** | Munich 🇩🇪 | Pod Expansion | Pod 4 arrives because Pods 1-3 weren't enough. Flat topology to keep things simple (this time). |
| **[05_llm_time](data/demos/05_llm_time/)** | Munich 🇩🇪 | Spine Expansion | Trojan Horse upgrade: Sneak in extra spines for LLM workloads and plausible deniability. |

Each scenario is a living example of how design-driven automation can handle vendor drama, topology chaos, and the occasional AI hype—while keeping you in control of your data model and your destiny.

## CI/CD
This project uses GitHub Actions for continuous integration. All pushes and pull requests are tested for lint, type checks, and unit tests.

## Security & Secrets
- Do not commit real API tokens. Use `.env` or GitHub secrets for sensitive data in production.
- Example tokens in this README are for demo purposes only.

## Troubleshooting
- If you encounter port conflicts, ensure no other service is running on port 8000.
- For dependency issues, run `uv sync` again.
- For Docker/infrahub issues, ensure Docker is running and you have the correct permissions.

## Testing
Run all tests using:
```bash
uv run inv validate
```
Or run specific test scripts in the [`tests/`](tests/) directory.

## 🙏 A Note from a Highly Fallible Carbon-Based Life Form

> Look, I'm just a human who occasionally writes code between coffee breaks and existential crises. If something explodes, catches fire, or simply refuses to work as advertised—like that one switch that's been "temporarily" in your rack since 2019—please [open an issue](https://github.com/t0m3kz/infrahub-demo/issues). I promise to investigate with the same determination I use to debug production on a Friday afternoon. Your feedback is invaluable, unlike my initial variable naming choices!

## Contributing
Contributions, questions, and feedback are welcome! Please use [GitHub Discussions][github-discussions-link].

## References
- [Infrahub Documentation](https://docs.infrahub.app)
- [Project Discussions](https://github.com/t0m3kz/infrahub-demo/discussions/)

## License
This project is licensed under the MIT License. See [LICENSE](LICENSE) for details.

[ruff-badge]:
<https://img.shields.io/endpoint?url=https://raw.githubusercontent.com/astral-sh/ruff/main/assets/badge/v2.json>
[ruff-link]:
(https://github.com/astral-sh/ruff)
[github-discussions-link]:
<https://github.com/t0m3kz/infrahub-demo/discussions/>
[github-discussions-badge]:
<https://img.shields.io/static/v1?label=Discussions&message=Ask&color=blue&logo=github>
[github-badge]:
<https://github.com/t0m3kz/infrahub-demo/actions/workflows/main.yml/badge.svg>
[github-link]:
<https://github.com/t0m3kz/infrahub-demo/actions/workflows/main.yml>
[coverage-badge]:
https://img.shields.io/codecov/c/github/t0m3kz/infrahub-demo?label=coverage
[coverage-link]:
https://codecov.io/gh/t0m3kz/infrahub-demo
[python-badge]:
<https://img.shields.io/badge/python-3.10%7C3.11%7C3.12-000000?logo=python>
[python-link]:
<https://www.python.org>
