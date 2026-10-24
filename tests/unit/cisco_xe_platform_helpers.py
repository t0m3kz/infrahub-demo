"""Shared helpers for the cisco_xe platform unit tests: smoke golden inputs and a bare BaseDeviceTransform."""

from __future__ import annotations

import copy
import json
from functools import cache
from pathlib import Path
from typing import Any

from transforms.common import BaseDeviceTransform

CONFIGS_DIR = Path(__file__).resolve().parents[1] / "smoke" / "configs"


def _golden_input(directory: str) -> dict[str, Any]:
    """Load a smoke golden's input.json (a fresh deep copy; the transform mutates what it is given)."""
    return copy.deepcopy(_load_json(directory))


@cache
def _load_json(directory: str) -> dict[str, Any]:
    return json.loads((CONFIGS_DIR / directory / "input.json").read_text())


def _make_transform(device_role: str) -> BaseDeviceTransform:
    """Instantiate BaseDeviceTransform bypassing InfrahubTransform __init__."""
    transform = BaseDeviceTransform.__new__(BaseDeviceTransform)
    transform.device_role = device_role
    transform.template_subdir = "leafs"
    transform.root_directory = "/fake/root"
    return transform
