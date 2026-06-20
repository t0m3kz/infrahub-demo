"""Infra tasks — Docker container lifecycle."""

import logging
import os
import time
from pathlib import Path
from typing import cast

from invoke import Collection, Context, Task, task

logging.basicConfig(level=logging.INFO, format="%(asctime)s  %(levelname)-8s  %(message)s")
log = logging.getLogger("infra")

INFRAHUB_ADDRESS = os.getenv("INFRAHUB_ADDRESS", "http://localhost:8000")
INFRAHUB_API_TOKEN = os.getenv("INFRAHUB_API_TOKEN", "admin")

_INFRAHUB_VERSION = os.getenv("VERSION", "latest")
COMPOSE_COMMAND = (
    "docker compose -p infrahub"
    if Path("docker-compose.yml").exists()
    else f"curl https://infrahub.opsmill.io/{_INFRAHUB_VERSION} | docker compose -p infrahub -f -"
)


def _check_container_running(context: Context, max_attempts: int = 60) -> bool:
    """Poll until infrahub-server reports (healthy) status."""
    log.info("Waiting for Infrahub server to be healthy...")
    for attempt in range(max_attempts):
        result = context.run(
            "docker ps --filter 'name=infrahub-infrahub-server' --filter 'status=running' "
            "--format 'table {{.Names}}\t{{.Status}}'",
            warn=True,
            hide=True,
            pty=True,
        )
        if result is not None and result.stdout and "(healthy)" in result.stdout:
            log.info("Infrahub server is healthy (attempt %d/%d)", attempt + 1, max_attempts)
            return True
        if attempt < max_attempts - 1:
            time.sleep(2)
            if (attempt + 1) % 10 == 0:
                log.info("Still waiting... (%ds elapsed)", (attempt + 1) * 2)
    log.error("Server failed to reach (healthy) after %ds", max_attempts * 2)
    return False


@task
def start(context: Context) -> None:
    """Start all Infrahub containers."""
    os.environ["INFRAHUB_ADDRESS"] = INFRAHUB_ADDRESS
    os.environ["INFRAHUB_API_TOKEN"] = INFRAHUB_API_TOKEN
    context.run(f"{COMPOSE_COMMAND} up -d", pty=True)


@task
def stop(context: Context) -> None:
    """Stop all Infrahub containers."""
    context.run(f"{COMPOSE_COMMAND} down", pty=True)


@task(optional=["component"])
def restart(context: Context, component: str = "") -> None:
    """Restart all (or a specific) container.

    Example:
        uv run invoke infra.restart
        uv run invoke infra.restart --component infrahub-server
    """
    context.run(f"{COMPOSE_COMMAND} restart {component}".strip(), pty=True)


@task
def destroy(context: Context) -> None:
    """Destroy all containers and volumes."""
    context.run(f"{COMPOSE_COMMAND} down -v", pty=True)


@task
def setup(context: Context) -> None:
    """Full environment setup: start containers, load schema, menu, and bootstrap data.

    Example:
        uv run invoke infra.setup
    """
    os.environ["INFRAHUB_ADDRESS"] = INFRAHUB_ADDRESS
    os.environ["INFRAHUB_API_TOKEN"] = INFRAHUB_API_TOKEN
    log.info("Starting Infrahub demo setup...")

    result = context.run(
        "docker ps --filter 'name=infrahub' --format '{{.Names}}'",
        warn=True,
        hide=True,
        pty=True,
    )
    if result is not None and result.stdout and result.stdout.strip():
        log.info("Infrahub containers already running")
    else:
        log.info("Starting containers...")
        start(context)
        if not _check_container_running(context):
            log.error("Infrahub container failed to start. Aborting.")
            return

    log.info("Loading schemas...")
    context.run("uv run infrahubctl schema load ./schemas/base --branch main", pty=True)
    context.run("uv run infrahubctl schema load ./schemas/extensions --branch main", pty=True)

    log.info("Loading menu...")
    context.run("uv run infrahubctl menu load menu --branch main", pty=True)

    log.info("Waiting before loading bootstrap data...")
    time.sleep(5)

    log.info("Loading bootstrap data...")
    context.run("uv run infrahubctl object load data/bootstrap/ --branch main", pty=True)

    log.info("Setup complete! Infrahub is ready.")


@task(optional=["ref"])
def register_repo(context: Context, ref: str = "routing") -> None:
    """Register the local repository and load event actions.

    Example:
        uv run invoke infra.register-repo
        uv run invoke infra.register-repo --ref main
    """
    os.environ["INFRAHUB_ADDRESS"] = INFRAHUB_ADDRESS
    os.environ["INFRAHUB_API_TOKEN"] = INFRAHUB_API_TOKEN

    log.info("Registering local repository (ref: %s)...", ref)
    context.run(
        f"uv run infrahubctl repository add test /upstream --ref {ref} --read-only",
        pty=True,
        warn=True,
    )

    log.info("Waiting for repository import to complete...")
    time.sleep(30)

    log.info("Loading event actions...")
    context.run("uv run infrahubctl object load data/events/ --branch main", pty=True, warn=True)

    log.info("Repository registration complete.")


ns = Collection("infra")
ns.add_task(cast(Task, start))
ns.add_task(cast(Task, stop))
ns.add_task(cast(Task, restart))
ns.add_task(cast(Task, destroy))
ns.add_task(cast(Task, setup))
ns.add_task(cast(Task, register_repo))
