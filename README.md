# Infrahub demo

[![Ruff][ruff-badge]][ruff-link]
[![Python][python-badge]][python-link]
[![Actions status][github-badge]][github-link]
[![Coverage Status][coverage-badge]][coverage-link]
[![GitHub Discussion][github-discussions-badge]][github-discussions-link]

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

### Install the Infrahub SDK
```bash
uv sync
```

### Start Infrahub
```bash
uv run invoke start
```

### Quick Setup (Recommended)
One command to setup everything:
```bash
uv run invoke setup
```

This loads schemas, bootstrap data, and menu. Then explore at http://localhost:8000

### Run Complete Demo
Deploy infrastructure and load demo data:
```bash
uv run invoke demo
```

Deploys DC-1 with servers and security configurations - ready to explore!

### Manual Setup (Alternative)

Load schemas
```bash
uv run invoke load-schema
```

Load demo data
```bash
uv run invoke load-objects
```

Load menu
```bash
uv run invoke load-menu
```

### Deploy Data Center Scenarios

Deploy any of 5 data centers with optional add-ons:
```bash
# List all available scenarios
uv run invoke list-scenarios

# Deploy DC-1 with default settings
uv run invoke deploy-dc --scenario dc1

# Deploy DC-3 with security configurations
uv run invoke deploy-dc --scenario dc3 --security

# Deploy DC-2 without servers
uv run invoke deploy-dc --scenario dc2 --servers=False
```

**Scenarios:**
- `dc1` - Large data center (Paris)
- `dc2` - Small-Medium data center (Frankfurt)
- `dc3` - Medium data center (London)
- `dc4` - Medium-Large data center (Amsterdam)
- `dc5` - Large data center (New York)

For complete invoke tasks documentation, see [INVOKE_TASKS_GUIDE.md](docs/INVOKE_TASKS_GUIDE.md)

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
uv run pytest
```
Or run specific test scripts in the [`tests/`](tests/) directory.

## Why Infrahub Demo?

> "The machine cannot be blamed. It is doing exactly what it was told. The real problem lies with those who blindly trust the vendor's GUI and their promises of automation."
>
> — *Inspired by Stanisław Lem's "Memoirs Found in a Bathtub"*

**My Journey:** I was tired. Tired of vendor tools that claimed to do "everything" but covered nothing. Tired of being forced into their web GUI dungeons, locked into their data structures, prisoner to their update cycles. These tools also came with monitoring and deployment engines that made the system incredibly demanding from a resources point of view—bloated, heavy, and requiring constant tuning. And don't get me started on managing YAML or Terraform files—it's a real nightmare, especially when you have so many dependencies. One change cascades into a hundred others, and tracking what affects what becomes nearly impossible at scale. Infrastructure as Code meant something different to me—**true independence**. Not just writing YAML files that some vendor tool interprets through their black box. I wanted **complete control over my data model**, the freedom to define my own relationships, the power to orchestrate everything through code—no "click here to continue" required.

Infrahub changed that. It's not another tool telling me how to think about infrastructure. It's a framework that lets **me define the rules**. Design-driven automation means I own the schemas, I own the logic, I own the destiny of my network. No more vendor lock-in, no more GUI dependency, no more compromise.

This demo is trying prove it: from topology design to device generation, from configuration templates to validation checks—all driven by **my data structures**, all automated through **my code**. That's the infrastructure revolution I was waiting for.

**Special thanks to [OpsMill](https://opsmill.com) for making this happen** – they built Infrahub with the vision that infrastructure teams should have complete control, not be prisoners to vendor constraints. (And yes, I'm bloody jealous I didn't have the power and motivation to come up with such a brilliant idea myself – like Prometheus watching others steal the fire of the gods!)

**To companies/vendors** who may be inspired by ideas from this repository and use them in their customer environments: please consider sponsoring 3 open source communities. It's crucial to ensure that the incredible volunteers who build these tools don't feel like losers. They deserve recognition and support for their outstanding contributions to the tech community. I sincerely hope you've earned enough that a few dollars a month sponsoring open source communities won't cause financial collapse – you can well afford to give back.

---

## 🙏 A Note from a Human

> I'm just a human making mistakes. If something is not working as expected, please [open an issue](https://github.com/t0m3kz/infrahub-demo/issues) and I'll do my best to get it sorted out. Your feedback is invaluable in making this project better!

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
<https://github.com/t0m3kz/infrahub-demo/actions/workflows/main.yml/badge.svg?branch=main>
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
