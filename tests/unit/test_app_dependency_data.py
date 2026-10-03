"""Consistency of the application data: AppDependency is the only place ports live.

The loader resolves every HFID, but only against a running Infrahub and only
in load order. These checks read the YAML alone, so a dependency with two
sources, a target endpoint that is never declared, or a prod component calling
a dev one fails here instead of halfway through a staged load.
"""

from __future__ import annotations

from pathlib import Path
from typing import Any

import pytest
import yaml

_ROOT = Path(__file__).parents[2]
_APP_DATA_DIRS = (
    _ROOT / "data" / "demos" / "30_all" / "07_applications",
    _ROOT / "data" / "demos" / "13_applications",
    _ROOT / "tests" / "integration" / "data" / "60_app_catalogue",
)
_REMOVED_KEYS = {"service_ports", "access_profile", "depends_on"}


def _documents(directory: Path) -> list[tuple[Path, dict[str, Any]]]:
    docs = []
    for path in sorted(directory.rglob("*.yml")):
        docs.extend((path, doc) for doc in yaml.safe_load_all(path.read_text()) if doc)
    return docs


def _entries(directory: Path, kind: str) -> list[dict[str, Any]]:
    return [entry for _, doc in _documents(directory) if doc["spec"]["kind"] == kind for entry in doc["spec"]["data"]]


def _catalogue(directory: Path) -> tuple[dict[str, str], dict[tuple[str, str], str]]:
    """Return component slug -> environment and (component slug, endpoint) -> endpoint_type."""
    components: dict[str, str] = {}
    endpoints: dict[tuple[str, str], str] = {}
    for app in _entries(directory, "AppApplication"):
        app_slug = f"{app['owner'].lower()}-{app['label']}-{app['environment']}"
        for component in app["children"]["data"]:
            slug = f"{app_slug}-{component['name']}"
            components[slug] = app["environment"]
            for endpoint in (component.get("children") or {}).get("data", []):
                endpoints[(slug, endpoint["name"])] = endpoint["endpoint_type"]
    for endpoint in _entries(directory, "AppEndpoint"):
        endpoints[(endpoint["parent"], endpoint["name"])] = endpoint["endpoint_type"]
    return components, endpoints


def _dependencies() -> list[tuple[Path, dict[str, Any]]]:
    return [(d, dep) for d in _APP_DATA_DIRS for dep in _entries(d, "AppDependency")]


def _ids(deps: list[tuple[Path, dict[str, Any]]]) -> list[str]:
    return [dep["name"] for _, dep in deps]


def _keys(node: Any) -> set[str]:
    if isinstance(node, dict):
        return set(node) | {k for value in node.values() for k in _keys(value)}
    if isinstance(node, list):
        return {k for item in node for k in _keys(item)}
    return set()


@pytest.mark.parametrize("directory", _APP_DATA_DIRS, ids=lambda d: d.name)
def test_no_removed_port_or_access_fields(directory: Path) -> None:
    """Endpoints and components carry no ports, profiles or depends_on lists."""
    for path, doc in _documents(directory):
        assert not _keys(doc) & _REMOVED_KEYS, path


@pytest.mark.parametrize(("directory", "dep"), _dependencies(), ids=_ids(_dependencies()))
def test_dependency_has_exactly_one_source(directory: Path, dep: dict[str, Any]) -> None:
    """A dependency comes from a component or from an access profile, never both or neither."""
    assert ("source" in dep) != ("source_profile" in dep)


@pytest.mark.parametrize(("directory", "dep"), _dependencies(), ids=_ids(_dependencies()))
def test_dependency_is_a_generator_target(directory: Path, dep: dict[str, Any]) -> None:
    """add_app_dependency only runs for a dependency in its target group, so
    one outside it never reconciles its application's rules."""
    assert "app_dependencies" in (dep.get("member_of_groups") or []), dep["name"]


@pytest.mark.parametrize("directory", _APP_DATA_DIRS, ids=lambda d: d.name)
def test_applications_and_components_are_generator_targets(directory: Path) -> None:
    """add_app_application and add_app_component only run for members of
    their target groups; the component run fans out to calling applications."""
    for app in _entries(directory, "AppApplication"):
        assert "app_applications" in (app.get("member_of_groups") or []), app["label"]
        for component in app["children"]["data"]:
            assert "app_components" in (component.get("member_of_groups") or []), (app["label"], component["name"])


@pytest.mark.parametrize(("directory", "dep"), _dependencies(), ids=_ids(_dependencies()))
def test_dependency_target_is_declared(directory: Path, dep: dict[str, Any]) -> None:
    """The target endpoint exists in the same data set."""
    _, endpoints = _catalogue(directory)
    assert tuple(dep["target"]) in endpoints


@pytest.mark.parametrize(("directory", "dep"), _dependencies(), ids=_ids(_dependencies()))
def test_dependency_carries_a_port_or_protocol(directory: Path, dep: dict[str, Any]) -> None:
    """With ports gone from endpoints, the dependency is the only place they can be."""
    assert dep.get("protocol"), dep["name"]
    if dep["protocol"] in ("tcp", "udp"):
        assert isinstance(dep.get("port_start"), int), dep["name"]


@pytest.mark.parametrize(("directory", "dep"), _dependencies(), ids=_ids(_dependencies()))
def test_access_profile_grants_target_private_access(directory: Path, dep: dict[str, Any]) -> None:
    """Only a private_access endpoint is published by a grant, and only a grant publishes it."""
    _, endpoints = _catalogue(directory)
    is_private = endpoints[tuple(dep["target"])] == "private_access"
    assert is_private == ("source_profile" in dep)


@pytest.mark.parametrize(("directory", "dep"), _dependencies(), ids=_ids(_dependencies()))
def test_dependency_stays_inside_one_environment(directory: Path, dep: dict[str, Any]) -> None:
    """Prod and non-prod are never connected, so a call never crosses environments."""
    if "source" not in dep:
        return
    components, _ = _catalogue(directory)
    target_component = dep["target"][0]
    if target_component not in components:
        pytest.skip(f"target component {target_component} is declared outside the app data")
    assert components[dep["source"]] == components[target_component]
