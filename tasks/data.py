"""Data tasks — schema, menu, and object loading."""

from typing import cast

from invoke import Collection, Context, Task, task


@task(optional=["schema", "branch"])
def load_schema(context: Context, schema: str = "./schemas/", branch: str = "main") -> None:
    """Load base and extension schemas.

    Example:
        uv run invoke data.load-schema
        uv run invoke data.load-schema --branch my-branch
    """
    context.run(f"uv run infrahubctl schema load {schema}/base --branch {branch}", pty=True)
    context.run(f"uv run infrahubctl schema load {schema}/extensions --branch {branch}", pty=True)


@task(optional=["branch"])
def load_menu(context: Context, menu: str = "menu", branch: str = "main") -> None:
    """Load the navigation menu.

    Example:
        uv run invoke data.load-menu
    """
    context.run(f"uv run infrahubctl menu load {menu} --branch {branch}", pty=True)


@task(optional=["branch"])
def load_objects(context: Context, path: str = "data/bootstrap/", branch: str = "main") -> None:
    """Load object YAML files from a path.

    Example:
        uv run invoke data.load-objects
        uv run invoke data.load-objects --path data/demos/100_full/01_dc/dc1
    """
    context.run(f"uv run infrahubctl object load {path} --branch {branch}", pty=True)


@task(optional=["branch"])
def load_data(context: Context, name: str = "bootstrap.py", branch: str = "main") -> None:
    """Run a bootstrap Python script.

    Example:
        uv run invoke data.load-data --name bootstrap.py
    """
    context.run(f"uv run infrahubctl run bootstrap/{name} --branch {branch}", pty=True)


ns = Collection("data")
ns.add_task(cast(Task, load_schema), name="load-schema")
ns.add_task(cast(Task, load_menu), name="load-menu")
ns.add_task(cast(Task, load_objects), name="load-objects")
ns.add_task(cast(Task, load_data), name="load-data")
