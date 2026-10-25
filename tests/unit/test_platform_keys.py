"""Guard: the platform NAME is the key of the config template and of every platform table in the code.

``DcimPlatform.name`` selects ``templates/configs/<role subdir>/<name>.j2`` (``BaseDeviceTransform._load_template``)
and is what the generators, transforms and checks compare against. Two spellings of one platform (``panos`` in the data,
``paloalto_panos`` in a table) silently skip the table, so this test ties data, templates and code tables together.
"""

from __future__ import annotations

import importlib
import pkgutil
from collections.abc import Iterator
from pathlib import Path
from typing import Any

import pytest
import yaml

import transforms.config
from checks.lisp_fabric import LISP_PLATFORM
from generators.firewall_context_spec import _VIRTUAL_TEMPLATE_PREFIX_BY_PLATFORM_AND_ROLE
from generators.helpers.interface_naming import _PLATFORM_NAMING
from generators.mlag import _IPV4_CONTROL_PLATFORMS, _NO_CONTROL_PLATFORMS
from transforms.common import UNFILTERED_BANNER_PLATFORMS, BaseDeviceTransform
from transforms.config.controller import _BUILDERS
from transforms.helpers.group_policy.dispatch import GPO_RENDERERS
from transforms.helpers.loadbalancer import _VENDORS
from transforms.helpers.ospf import OSPF_AUTH_MODES
from transforms.helpers.overlay import OVERLAY_RENDERERS
from transforms.helpers.proxy import _DESTINATION_TYPES, _TLS_INSPECTING_RENDERERS
from transforms.helpers.vxlan import _PLATFORM_VXLAN_KEYS

# The roles of the transforms that set no ``device_role`` (it is the VXLAN knob of the fabric transforms).
_SERVICE_ROLE_BY_SUBDIR = {"firewalls": "firewall", "proxies": "proxy", "loadbalancers": "load-balancer"}
# Template directories under templates/configs/ that are not a role subdirectory keyed by platform.
_NOT_ROLE_SUBDIRS = {"common", "equinix", "loadbalancers_cloud", "proxies_cloud"}


def _device_transforms() -> list[type[BaseDeviceTransform]]:
    """Every BaseDeviceTransform subclass of transforms/config, imported."""
    for module in pkgutil.iter_modules(transforms.config.__path__):
        importlib.import_module(f"transforms.config.{module.name}")
    found: list[type[BaseDeviceTransform]] = []
    pending = list(BaseDeviceTransform.__subclasses__())
    while pending:
        cls = pending.pop()
        pending.extend(cls.__subclasses__())
        if cls.template_subdir:
            found.append(cls)
    return found


def _subdir_by_role() -> dict[str, str]:
    """Device role -> the templates/configs subdirectory of the transform rendering it."""
    roles: dict[str, str] = {}
    for cls in _device_transforms():
        role = cls.device_role or _SERVICE_ROLE_BY_SUBDIR.get(cls.template_subdir)
        if role:
            roles[role] = cls.template_subdir
    return roles


def _platform_role_pairs(node: Any) -> Iterator[tuple[str, str | None]]:
    """Every (platform, role) of a loaded object: a ``platform`` string and the ``role`` beside it."""
    if isinstance(node, dict):
        platform = node.get("platform")
        if isinstance(platform, str):
            role = node.get("role")
            yield platform, role if isinstance(role, str) else None
        for value in node.values():
            yield from _platform_role_pairs(value)
    elif isinstance(node, list):
        for item in node:
            yield from _platform_role_pairs(item)


@pytest.fixture(scope="module")
def platform_names(root_dir: Path) -> set[str]:
    """The names in data/bootstrap/05_platforms.yml."""
    document = yaml.safe_load((root_dir / "data/bootstrap/05_platforms.yml").read_text())
    return {entry["name"] for entry in document["spec"]["data"]}


@pytest.fixture(scope="module")
def data_platform_roles(root_dir: Path) -> dict[str, set[str | None]]:
    """Platform -> the roles it is used with, over every object file under data/ and tests/integration/data."""
    used: dict[str, set[str | None]] = {}
    paths = [
        path
        for base in ("data", "tests/integration/data")
        if (root_dir / base).is_dir()
        for pattern in ("*.yml", "*.yaml")
        for path in (root_dir / base).rglob(pattern)
    ]
    for path in paths:
        for document in yaml.safe_load_all(path.read_text()):
            for platform, role in _platform_role_pairs(document):
                used.setdefault(platform, set()).add(role)
    return used


def test_every_platform_the_data_names_is_defined(
    platform_names: set[str], data_platform_roles: dict[str, set[str | None]]
) -> None:
    """A device, template or software image naming a platform that 05_platforms.yml does not define cannot load."""
    assert set(data_platform_roles) - platform_names == set()


# (platform, role) pairs of the bootstrap catalog whose role has a config transform but whose platform has no template
# for it today (a catalog row never instantiated by a deployment, or a device the artifact cannot render). Each is a gap
# to close with a template, not a pattern to copy: the test below fails once one is closed, so the entry goes with it.
_CATALOG_WITHOUT_TEMPLATE = {
    ("cisco_ios", "access-switch"),
    ("juniper_junos", "edge"),
    ("linux", "edge"),
    ("linux", "load-balancer"),
}


def _pairs_without_template(root_dir: Path, data_platform_roles: dict[str, set[str | None]]) -> set[tuple[str, str]]:
    """The (platform, role) pairs of the data whose role has a config transform and whose template file is missing."""
    subdir_by_role = _subdir_by_role()
    return {
        (platform, role)
        for platform, roles in data_platform_roles.items()
        for role in roles
        if role in subdir_by_role
        and not (root_dir / "templates/configs" / subdir_by_role[role] / f"{platform}.j2").is_file()
    }


def test_every_role_with_a_transform_has_a_template_per_platform_in_the_data(
    root_dir: Path, data_platform_roles: dict[str, set[str | None]]
) -> None:
    """A platform used for a role that has a config transform needs templates/configs/<subdir>/<platform>.j2.

    The roles without a transform (servers, controllers, console servers, ...) are derived from the transforms, not
    listed here: such a platform has no config template, by design.
    """
    assert _pairs_without_template(root_dir, data_platform_roles) - _CATALOG_WITHOUT_TEMPLATE == set()


def test_the_catalog_gaps_are_still_gaps(root_dir: Path, data_platform_roles: dict[str, set[str | None]]) -> None:
    """An entry of _CATALOG_WITHOUT_TEMPLATE whose template exists (or whose row left the data) is removed."""
    assert _CATALOG_WITHOUT_TEMPLATE - _pairs_without_template(root_dir, data_platform_roles) == set()


def test_every_config_template_is_named_after_a_platform(root_dir: Path, platform_names: set[str]) -> None:
    """A template file whose name is no platform name is never loaded."""
    strays = sorted(
        path.relative_to(root_dir).as_posix()
        for directory in (root_dir / "templates/configs").iterdir()
        if directory.is_dir() and directory.name not in _NOT_ROLE_SUBDIRS
        for path in directory.glob("*.j2")
        if path.stem not in platform_names
    )
    assert strays == []


_ROOT = Path(__file__).parent.parent.parent


def _code_table_platforms() -> dict[str, set[str]]:
    """Each code table that is matched against ``platform.name`` -> the platform names it holds."""
    # The proxy tables also key the cloud renderers (a template of proxies_cloud/), which are no platform.
    cloud_proxy_renderers = {path.stem for path in (_ROOT / "templates/configs/proxies_cloud").glob("*.j2")}
    external_policy = set().union(*(cls.external_policy_platforms for cls in _device_transforms()))
    return {
        "firewall_context_spec._VIRTUAL_TEMPLATE_PREFIX_BY_PLATFORM_AND_ROLE": {
            platform for platform, _ in _VIRTUAL_TEMPLATE_PREFIX_BY_PLATFORM_AND_ROLE
        },
        "controller._BUILDERS": {platform for _, platform in _BUILDERS if platform},
        "helpers.loadbalancer._VENDORS": set(_VENDORS),
        "helpers.proxy._TLS_INSPECTING_RENDERERS": set(_TLS_INSPECTING_RENDERERS) - cloud_proxy_renderers,
        "helpers.proxy._DESTINATION_TYPES": set(_DESTINATION_TYPES) - cloud_proxy_renderers,
        "mlag._IPV4_CONTROL_PLATFORMS": set(_IPV4_CONTROL_PLATFORMS),
        "mlag._NO_CONTROL_PLATFORMS": set(_NO_CONTROL_PLATFORMS),
        "common.UNFILTERED_BANNER_PLATFORMS": set(UNFILTERED_BANNER_PLATFORMS),
        "BaseDeviceTransform.external_policy_platforms": external_policy,
        "helpers.vxlan._PLATFORM_VXLAN_KEYS": set(_PLATFORM_VXLAN_KEYS),
        "interface_naming._PLATFORM_NAMING": set(_PLATFORM_NAMING),
        "helpers.ospf.OSPF_AUTH_MODES": set(OSPF_AUTH_MODES),
        "helpers.overlay.OVERLAY_RENDERERS": set().union(*OVERLAY_RENDERERS.values()),
        "group_policy.dispatch.GPO_RENDERERS": set().union(*GPO_RENDERERS.values()),
        "checks.lisp_fabric.LISP_PLATFORM": {LISP_PLATFORM},
    }


@pytest.mark.parametrize("table", sorted(_code_table_platforms()))
def test_every_platform_a_code_table_names_is_defined(table: str, platform_names: set[str]) -> None:
    """A table keyed by a spelling that is no platform name never matches (``panos`` against ``paloalto_panos``)."""
    assert _code_table_platforms()[table] - platform_names == set()
