"""Jinja2 template loading shared by the transforms."""

from collections.abc import Callable
from typing import Any

from jinja2 import Environment, FileSystemLoader, Template


def load_template(
    search_path: str,
    name: str,
    *,
    filters: dict[str, Callable[..., Any]] | None = None,
    autoescape: Any = False,
    keep_trailing_newline: bool = True,
    trim_blocks: bool = False,
) -> Template:
    """Load template `name` from `search_path` with the given environment settings.

    `trim_blocks` also strips the whitespace before a block tag (lstrip_blocks), so a block tag on its own line
    leaves no line behind.
    """
    env = Environment(
        loader=FileSystemLoader(search_path),
        autoescape=autoescape,
        keep_trailing_newline=keep_trailing_newline,
        trim_blocks=trim_blocks,
        lstrip_blocks=trim_blocks,
    )
    if filters:
        env.filters.update(filters)
    return env.get_template(name)
