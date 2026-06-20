"""Invoke tasks for the infrahub-demo project.

Namespaces:
  dev     — code quality, linting, tests
  infra   — Docker container lifecycle
  data    — schema, menu, and object loading
  demo    — end-to-end demo flows

Top-level shortcuts (aliases for namespaced tasks):
  destroy             — infra.destroy
  setup               — infra.setup
  validate            — dev.validate
  setup-precommit     — dev.setup-precommit
  test-unit           — dev.test-unit
  test-integration    — dev.test-integration
  clean-testcontainers — dev.clean-testcontainers
  load-schema         — data.load-schema
  load-menu           — data.load-menu
  load-objects        — data.load-objects
  load-data           — data.load-data
  register-repo       — infra.register-repo
  deploy-dc           — demo.deploy-dc
  run-demo            — demo.run-demo
"""

from typing import cast

from invoke import Collection, Task
from invoke import task as _task

from tasks import data, demo, dev, infra


@_task
def destroy(context):
    """Destroy all containers and volumes (alias for infra.destroy)."""
    infra.destroy(context)


@_task
def setup(context):
    """Full environment setup (alias for infra.setup)."""
    infra.setup(context)


@_task
def validate(context):
    """Run linting and type checks (alias for dev.validate)."""
    dev.validate(context)


@_task
def setup_precommit(context):
    """Install pre-commit hooks (alias for dev.setup-precommit)."""
    dev.setup_precommit(context)


@_task(optional=["basetemp"])
def test_unit(context, basetemp=".pytest-tmp"):
    """Run unit tests (alias for dev.test-unit)."""
    dev.test_unit(context, basetemp=basetemp)


@_task(optional=["basetemp", "server_port"])
def test_integration(context, basetemp=".pytest-tmp", server_port=8000):
    """Run integration tests (alias for dev.test-integration)."""
    dev.test_integration(context, basetemp=basetemp, server_port=server_port)


@_task
def clean_testcontainers(context):
    """Remove leftover test containers (alias for dev.clean-testcontainers)."""
    dev.clean_testcontainers(context)


@_task(optional=["schema", "branch"])
def load_schema(context, schema="./schemas/", branch="main"):
    """Load schema into Infrahub (alias for data.load-schema)."""
    data.load_schema(context, schema=schema, branch=branch)


@_task(optional=["branch"])
def load_menu(context, menu="menu", branch="main"):
    """Load navigation menu (alias for data.load-menu)."""
    data.load_menu(context, menu=menu, branch=branch)


@_task(optional=["branch"])
def load_objects(context, path="data/bootstrap/", branch="main"):
    """Load bootstrap objects (alias for data.load-objects)."""
    data.load_objects(context, path=path, branch=branch)


@_task(optional=["branch"])
def load_data(context, name="bootstrap.py", branch="main"):
    """Run a data-loading script (alias for data.load-data)."""
    data.load_data(context, name=name, branch=branch)


@_task(optional=["ref"])
def register_repo(context, ref="routing"):
    """Register the local repository and load event actions (alias for infra.register-repo)."""
    infra.register_repo(context, ref=ref)


@_task(optional=["scenario", "branch"])
def deploy_dc(context, scenario="dc1", branch="main"):
    """Deploy datacenter topology (alias for demo.deploy-dc)."""
    demo.deploy_dc(context, scenario=scenario, branch=branch)


@_task(optional=["phases", "skip_generators", "skip_merge", "dry_run", "dcs"])
def run_demo(context, phases="", skip_generators=False, skip_merge=False, dry_run=False, dcs=""):
    """Run end-to-end demo flow (alias for demo.run-demo)."""
    demo.run_demo(
        context, phases=phases, skip_generators=skip_generators, skip_merge=skip_merge, dry_run=dry_run, dcs=dcs
    )


ns = Collection()
ns.add_task(cast(Task, destroy))
ns.add_task(cast(Task, setup))
ns.add_task(cast(Task, validate))
ns.add_task(cast(Task, setup_precommit))
ns.add_task(cast(Task, test_unit))
ns.add_task(cast(Task, test_integration))
ns.add_task(cast(Task, clean_testcontainers))
ns.add_task(cast(Task, load_schema))
ns.add_task(cast(Task, load_menu))
ns.add_task(cast(Task, load_objects))
ns.add_task(cast(Task, load_data))
ns.add_task(cast(Task, register_repo))
ns.add_task(cast(Task, deploy_dc))
ns.add_task(cast(Task, run_demo))
ns.add_collection(dev.ns)
ns.add_collection(infra.ns)
ns.add_collection(data.ns)
ns.add_collection(demo.ns)
