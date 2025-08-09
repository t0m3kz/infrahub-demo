"""Tasks for the infrahub-cdc project."""

import os

from invoke import Context, task  # type: ignore

INFRAHUB_VERSION = os.getenv("INFRAHUB_VERSION", "")
COMPOSE_COMMAND = f"curl https://infrahub.opsmill.io/{INFRAHUB_VERSION} | docker compose -p infrahub -f -"


@task
def start(context: Context) -> None:
    """Start all containers."""
    context.run(f"{COMPOSE_COMMAND} up -d")


@task(optional=["schema", "branch"])
def load_schema(
    context: Context, schema: str = "./schemas/", branch: str = "main"
) -> None:
    """Load the schemas from the given path."""
    context.run(f"infrahubctl schema load {schema} --branch {branch}")


@task(optional=["branch"])
def load_data(
    context: Context, name: str = "bootstrap.py", branch: str = "main"
) -> None:
    """Load the data from the given path."""
    context.run(f"infrahubctl run bootstrap/{name} --branch {branch}")


@task(optional=["branch"])
def load_menu(context: Context, menu: str = "menu", branch: str = "main") -> None:
    """Load the menu from the given path."""
    context.run(f"infrahubctl menu load {menu} --branch {branch}")


@task(optional=["branch"])
def load_objects(
    context: Context, path: str = "data/bootstrap/", branch: str = "main"
) -> None:
    """Load objects from the given path."""
    context.run(f"infrahubctl object load {path} --branch {branch}")


@task
def destroy(context: Context) -> None:
    """Destroy all containers."""
    context.run(f"{COMPOSE_COMMAND} down -v")


@task
def stop(context: Context) -> None:
    """Stop all containers."""
    context.run(f"{COMPOSE_COMMAND} down")


@task
def restart(context: Context, component: str = "") -> None:
    """Stop all containers."""
    if component:
        context.run(f"{COMPOSE_COMMAND} restart {component}")
        return

    context.run(f"{COMPOSE_COMMAND} restart")


@task
def run_tests(context: Context) -> None:
    """Run all tests."""
    context.run("pytest -vv tests")


@task
def validate(context: Context) -> None:
    """Run all code quality tests."""
    context.run("ruff check . --fix")
    context.run("pytest -vv tests")
