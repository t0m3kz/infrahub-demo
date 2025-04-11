"""Tasks for the infrahub-cdc project."""

import os

from invoke import task, Context  # type: ignore

INFRAHUB_VERSION = os.getenv("INFRAHUB_VERSION", "")
COMPOSE_COMMAND = (
    f"curl https://infrahub.opsmill.io/{INFRAHUB_VERSION} | docker compose -f -"
)


@task
def start(context: Context) -> None:
    """Start all containers."""
    context.run(f"{COMPOSE_COMMAND} up -d")


@task(optional=["schema", "branch"])
def load_schema(
    context: Context, schema: str = "./model/", branch: str = "main"
) -> None:
    """Load the schemas from the given path."""
    context.run(f"infrahubctl schema load {schema} --branch {branch}")


@task(optional=["branch"])
def load_data(
    context: Context, name: str = "bootstrap.py", branch: str = "main"
) -> None:
    """Load the data from the given path."""

    context.run(f"infrahubctl run bootstrap/{name} --branch {branch}")


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
    """Run all code qality tests."""
    context.run("pre-commit run --all-files")
