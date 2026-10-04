"""Consistency of the application data: components list ports, dependencies open them.

An AppComponent is addressed by its fqdn (also its HFID) and lists the ports
it listens on. An AppDependency comes from a component or an access profile
and goes to a component (opening all of its ports, or the subset it lists) or
to an external target_fqdn (listing the ports it needs).

The loader resolves every HFID, but only against a running Infrahub and only
in load order, and checks/app_dependency.py only runs on a proposed change.
These checks read the YAML alone, so a dependency with two sources, a target
that is never declared, a port the target does not listen on, or a prod
component calling a dev one fails here instead of halfway through a staged
load.
"""

from __future__ import annotations

from collections.abc import Callable
from dataclasses import dataclass, field
from pathlib import Path
from typing import Any

import pytest
import yaml

from utils.ports import PortProfileHelper, PortSpec

_ROOT = Path(__file__).parents[2]
_APP_DATA_DIRS = (
    _ROOT / "data" / "demos" / "30_all" / "07_applications",
    _ROOT / "data" / "demos" / "13_applications",
    _ROOT / "tests" / "integration" / "data" / "60_app_catalogue",
)
_REMOVED_KEYS = {
    "slug",
    "load_balancer",
    "protocol",
    "port_start",
    "port_end",
    "endpoint_type",
    "service_ports",
    "access_profile",
    "depends_on",
}


@dataclass(frozen=True)
class _Component:
    """An AppComponent as declared under its AppApplication."""

    directory: Path
    owner: str
    application: str
    environment: str
    raw: dict[str, Any] = field(hash=False)

    @property
    def fqdn(self) -> str:
        return self.raw.get("fqdn", "")

    @property
    def identity(self) -> tuple[str, str, str, str]:
        """(owner, application label, environment, component name)."""
        return self.owner, self.application, self.environment, self.raw["name"]


def _documents(directory: Path) -> list[tuple[Path, dict[str, Any]]]:
    docs = []
    for path in sorted(directory.rglob("*.yml")):
        docs.extend((path, doc) for doc in yaml.safe_load_all(path.read_text()) if doc)
    return docs


def _entries(directory: Path, kind: str) -> list[dict[str, Any]]:
    return [entry for _, doc in _documents(directory) if doc["spec"]["kind"] == kind for entry in doc["spec"]["data"]]


def _components(directory: Path) -> list[_Component]:
    return [
        _Component(directory, app["owner"], app["label"], app["environment"], component)
        for app in _entries(directory, "AppApplication")
        for component in app["children"]["data"]
    ]


def _catalogue(directory: Path) -> dict[str, _Component]:
    """Component fqdn -> component, for the components declared in directory."""
    return {component.fqdn: component for component in _components(directory)}


def _egress_owners(directory: Path) -> set[str]:
    """Customers in directory that are assigned an egress_service."""
    return {c["org_id"] for c in _entries(directory, "OrganizationCustomer") if c.get("egress_service")}


def _ports(specs: list[str] | None) -> list[PortSpec]:
    return [PortProfileHelper.parse_port_spec(spec) for spec in specs or []]


def _covers(listened: PortSpec, port: PortSpec) -> bool:
    protocol, start, end = port
    l_protocol, l_start, l_end = listened
    return protocol == l_protocol and l_start <= start and (end or start) <= (l_end or l_start)


def _keys(node: Any) -> set[str]:
    if isinstance(node, dict):
        return set(node) | {k for value in node.values() for k in _keys(value)}
    if isinstance(node, list):
        return {k for item in node for k in _keys(item)}
    return set()


def _kinds(node: Any) -> set[str]:
    if isinstance(node, dict):
        own = {node["kind"]} if isinstance(node.get("kind"), str) else set()
        return own | {k for value in node.values() for k in _kinds(value)}
    if isinstance(node, list):
        return {k for item in node for k in _kinds(item)}
    return set()


_DEPENDENCIES = [(d, dep) for d in _APP_DATA_DIRS for dep in _entries(d, "AppDependency")]
_DEPENDENCY_IDS = [f"{d.name}/{dep['name']}" for d, dep in _DEPENDENCIES]
_COMPONENTS = [component for d in _APP_DATA_DIRS for component in _components(d)]
_COMPONENT_IDS = [f"{c.directory.name}/{c.fqdn or c.raw['name']}" for c in _COMPONENTS]


def _select(predicate: Callable[[dict[str, Any]], bool]) -> tuple[list[tuple[Path, dict[str, Any]]], list[str]]:
    """The dependencies matching predicate, with their test ids."""
    selected = [(d, dep) for d, dep in _DEPENDENCIES if predicate(dep)]
    return selected, [f"{d.name}/{dep['name']}" for d, dep in selected]


_TO_COMPONENT, _TO_COMPONENT_IDS = _select(lambda dep: "target" in dep)
_TO_FQDN, _TO_FQDN_IDS = _select(lambda dep: "target_fqdn" in dep)
_COMPONENT_TO_FQDN, _COMPONENT_TO_FQDN_IDS = _select(lambda dep: "target_fqdn" in dep and "source" in dep)
_COMPONENT_TO_COMPONENT, _COMPONENT_TO_COMPONENT_IDS = _select(lambda dep: "target" in dep and "source" in dep)
_GRANTS, _GRANT_IDS = _select(lambda dep: "source_profile" in dep)


def test_the_scan_finds_the_app_data() -> None:
    """Guard against the loaders silently finding nothing in any directory."""
    for directory in _APP_DATA_DIRS:
        assert _components(directory), directory
        assert _entries(directory, "AppDependency"), directory


@pytest.mark.parametrize("directory", _APP_DATA_DIRS, ids=lambda d: d.name)
def test_no_removed_kinds_or_fields(directory: Path) -> None:
    """AppEndpoint is gone, and so are slugs, per-dependency protocol/port_start/port_end
    and the older ports/profile/depends_on fields."""
    for path, doc in _documents(directory):
        assert "AppEndpoint" not in _kinds(doc), path
        assert not _keys(doc) & _REMOVED_KEYS, (path, _keys(doc) & _REMOVED_KEYS)


@pytest.mark.parametrize("component", _COMPONENTS, ids=_COMPONENT_IDS)
def test_component_is_a_leaf_with_an_fqdn(component: _Component) -> None:
    """A component has no children and is addressed by its fqdn."""
    assert "children" not in component.raw, component.identity
    assert component.fqdn, component.identity


@pytest.mark.parametrize("directory", _APP_DATA_DIRS, ids=lambda d: d.name)
def test_component_fqdns_are_unique(directory: Path) -> None:
    """fqdn is the component HFID, so two components can never share one."""
    fqdns = [component.fqdn for component in _components(directory)]
    duplicates = sorted({fqdn for fqdn in fqdns if fqdns.count(fqdn) > 1})
    assert not duplicates


def test_shared_fqdn_across_data_sets_is_the_same_component() -> None:
    """A component declared in two data sets (c001 checkout) must be the same
    component in both, or one load would rename the other's tier."""
    seen: dict[str, _Component] = {}
    for component in _COMPONENTS:
        first = seen.setdefault(component.fqdn, component)
        assert first.identity == component.identity, component.fqdn


@pytest.mark.parametrize("component", _COMPONENTS, ids=_COMPONENT_IDS)
def test_component_ports_parse(component: _Component) -> None:
    """Every component port is protocol/port or protocol/start-end."""
    assert isinstance(component.raw.get("ports", []), list), component.fqdn
    _ports(component.raw.get("ports"))


@pytest.mark.parametrize(("directory", "dep"), _DEPENDENCIES, ids=_DEPENDENCY_IDS)
def test_dependency_ports_parse(directory: Path, dep: dict[str, Any]) -> None:
    """Every dependency port is protocol/port or protocol/start-end."""
    assert isinstance(dep.get("ports", []), list), dep["name"]
    _ports(dep.get("ports"))


@pytest.mark.parametrize(("directory", "dep"), _DEPENDENCIES, ids=_DEPENDENCY_IDS)
def test_dependency_has_exactly_one_source(directory: Path, dep: dict[str, Any]) -> None:
    """A dependency comes from a component or from an access profile, never both or neither."""
    assert ("source" in dep) != ("source_profile" in dep)


@pytest.mark.parametrize(("directory", "dep"), _DEPENDENCIES, ids=_DEPENDENCY_IDS)
def test_dependency_has_exactly_one_target(directory: Path, dep: dict[str, Any]) -> None:
    """A dependency goes to a component or to an external fqdn, never both or neither."""
    assert ("target" in dep) != ("target_fqdn" in dep)


@pytest.mark.parametrize(("directory", "dep"), _DEPENDENCIES, ids=_DEPENDENCY_IDS)
def test_dependency_source_and_target_are_declared(directory: Path, dep: dict[str, Any]) -> None:
    """source and target are component fqdns declared in the same data set."""
    components = _catalogue(directory)
    for side in ("source", "target"):
        if side in dep:
            assert isinstance(dep[side], str), (dep["name"], side)
            assert dep[side] in components, (dep["name"], side, dep[side])


@pytest.mark.parametrize(("directory", "dep"), _TO_FQDN, ids=_TO_FQDN_IDS)
def test_target_fqdn_dependency_lists_ports(directory: Path, dep: dict[str, Any]) -> None:
    """An external target has no ports of its own, so the dependency must name them."""
    assert dep.get("ports"), dep["name"]
    assert dep["target_fqdn"] not in _catalogue(directory), "a declared component is a target, not a target_fqdn"


@pytest.mark.parametrize(("directory", "dep"), _COMPONENT_TO_FQDN, ids=_COMPONENT_TO_FQDN_IDS)
def test_target_fqdn_dependency_owner_has_an_egress_service(directory: Path, dep: dict[str, Any]) -> None:
    """A target_fqdn is reached through the source owner's egress proxy, so the owner needs one."""
    owner = _catalogue(directory)[dep["source"]].owner
    assert owner in _egress_owners(directory), (dep["name"], owner)


@pytest.mark.parametrize(("directory", "dep"), _TO_COMPONENT, ids=_TO_COMPONENT_IDS)
def test_dependency_ports_are_listened_on_by_the_target(directory: Path, dep: dict[str, Any]) -> None:
    """A dependency may narrow its target's ports, never widen them."""
    target_ports = _ports(_catalogue(directory)[dep["target"]].raw.get("ports"))
    for port in _ports(dep.get("ports")):
        assert any(_covers(listened, port) for listened in target_ports), (dep["name"], port)


@pytest.mark.parametrize(("directory", "dep"), _TO_COMPONENT, ids=_TO_COMPONENT_IDS)
def test_dependency_opens_at_least_one_port(directory: Path, dep: dict[str, Any]) -> None:
    """Without ports of its own a dependency opens its target's, so one of the two must list some."""
    assert dep.get("ports") or _catalogue(directory)[dep["target"]].raw.get("ports"), dep["name"]


@pytest.mark.parametrize(("directory", "dep"), _GRANTS, ids=_GRANT_IDS)
def test_access_profile_grants_target_a_component(directory: Path, dep: dict[str, Any]) -> None:
    """A grant publishes a component through the ZTNA broker; it never opens an external fqdn."""
    assert "target" in dep, dep["name"]
    assert "target_fqdn" not in dep, dep["name"]


@pytest.mark.parametrize("directory", _APP_DATA_DIRS, ids=lambda d: d.name)
def test_one_dependency_per_source_and_target(directory: Path) -> None:
    """Two flows between the same pair are one dependency with both ports."""
    pairs = [
        (dep.get("source") or dep.get("source_profile"), dep.get("target") or dep.get("target_fqdn"))
        for dep in _entries(directory, "AppDependency")
    ]
    duplicates = sorted({pair for pair in pairs if pairs.count(pair) > 1})
    assert not duplicates


@pytest.mark.parametrize(("directory", "dep"), _COMPONENT_TO_COMPONENT, ids=_COMPONENT_TO_COMPONENT_IDS)
def test_dependency_stays_inside_one_environment(directory: Path, dep: dict[str, Any]) -> None:
    """Prod and non-prod are never connected, so a call never crosses environments."""
    components = _catalogue(directory)
    assert components[dep["source"]].environment == components[dep["target"]].environment


@pytest.mark.parametrize(("directory", "dep"), _DEPENDENCIES, ids=_DEPENDENCY_IDS)
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
    for component in _components(directory):
        assert "app_components" in (component.raw.get("member_of_groups") or []), component.identity
