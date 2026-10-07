"""Unit tests for CheckAppDependency (checks/app_dependency.py).

The check receives the raw app_dependency_validation GraphQL payload
(queries/validation/app_dependency.gql), so the fixtures below are built in
that edges/node/value shape and go through clean_data like the real query.
"""

from __future__ import annotations

from typing import Any, cast

import pytest

from checks.app_dependency import CheckAppDependency

# ---------------------------------------------------------------------------
# Harness and payload builders (raw GraphQL shape)
# ---------------------------------------------------------------------------

_UNSET = object()


def _check() -> Any:
    check = cast(Any, CheckAppDependency.__new__(CheckAppDependency))
    errors: list[str] = []
    check._captured_errors = errors
    check.log_error = lambda message: errors.append(message)
    return check


def _component(fqdn: str, ports: list[str] | None) -> dict[str, Any]:
    """A target component node as the query returns it."""
    return {"id": f"id-{fqdn}", "fqdn": {"value": fqdn}, "ports": {"value": ports}}


def _source(fqdn: str = "backend.c001.example", *, egress: bool = True, owner: bool = True) -> dict[str, Any]:
    """A source component node with its application's owner and egress_service."""
    owner_node: dict[str, Any] | None = None
    if owner:
        owner_node = {
            "name": {"value": "c001-customer"},
            "egress_service": {"node": {"id": "proxy-1"} if egress else None},
        }
    parent_node = {"owner": {"node": owner_node}} if owner else None
    return {"id": f"id-{fqdn}", "fqdn": {"value": fqdn}, "parent": {"node": parent_node}}


def _dependency(
    name: str = "backend-to-db",
    *,
    ports: list[str] | None = None,
    target_fqdn: str | None = None,
    source: Any = _UNSET,
    source_profile: bool = False,
    target: dict[str, Any] | None = None,
) -> dict[str, Any]:
    """A dependency node; by default a component source with no target set."""
    source_node = _source() if source is _UNSET else source
    return {
        "name": {"value": name},
        "ports": {"value": ports},
        "target_fqdn": {"value": target_fqdn},
        "source_profile": {"node": {"id": "profile-1"} if source_profile else None},
        "source": {"node": source_node},
        "target": {"node": target},
    }


def _payload(
    dependencies: list[dict[str, Any]] | None = None,
    components: list[dict[str, Any]] | None = None,
) -> dict[str, Any]:
    return {
        "AppComponent": {
            "edges": [{"node": {"fqdn": c["fqdn"], "ports": c["ports"]}} for c in components or []],
        },
        "AppDependency": {"edges": [{"node": dep} for dep in dependencies or []]},
    }


def _errors(*dependencies: dict[str, Any], components: list[dict[str, Any]] | None = None) -> list[str]:
    check = _check()
    check.validate(_payload(list(dependencies), components))
    return check._captured_errors


DB = _component("db.c001.example", ["tcp/5432"])
CACHE = _component("cache.c001.example", ["tcp/6379", "udp/30000-30010"])


# ===========================================================================
# Clean payloads
# ===========================================================================


class TestCleanPayload:
    def test_query_name_matches_the_registered_query(self) -> None:
        """.infrahub.yml registers the check against app_dependency_validation."""
        assert CheckAppDependency.query == "app_dependency_validation"

    def test_empty_payload_has_no_errors(self) -> None:
        """No components and no dependencies is valid."""
        check = _check()
        check.validate({"AppComponent": {"edges": []}, "AppDependency": {"edges": []}})
        assert check._captured_errors == []

    def test_missing_top_level_keys_have_no_errors(self) -> None:
        """A payload without either kind is valid, not a crash."""
        check = _check()
        check.validate({})
        assert check._captured_errors == []

    def test_fully_valid_payload_has_no_errors(self) -> None:
        """Every valid dependency shape side by side passes cleanly."""
        dependencies = [
            # component -> component, every target port (empty ports)
            _dependency("be-to-cache", target=CACHE),
            # component -> component, narrowed to a listed port
            _dependency("be-to-db", ports=["tcp/5432"], target=DB),
            # component -> component, a sub-range of a listed range
            _dependency("be-to-cache-gossip", ports=["udp/30002-30005", "udp/30010"], target=CACHE),
            # component -> external fqdn through the owner's egress service
            _dependency("be-to-stripe", ports=["tcp/443"], target_fqdn="api.stripe.com"),
            # access-profile grant -> component
            _dependency("web-private-access", source=None, source_profile=True, target=DB),
            # target declares no ports but the dependency lists its own
            _dependency("be-to-legacy", ports=["tcp/8080"], target=_component("legacy.c001.example", None)),
        ]

        assert _errors(*dependencies, components=[DB, CACHE]) == []


# ===========================================================================
# Component ports
# ===========================================================================


class TestComponentPorts:
    @pytest.mark.parametrize("bad", ["icmp/1", "tcp/0", "tcp/65536", "tcp/9000-8000", "garbage"])
    def test_malformed_component_port_is_reported(self, bad: str) -> None:
        """A component port that does not parse is an error on that component."""
        errors = _errors(components=[_component("api.c001.example", ["tcp/443", bad])])

        assert len(errors) == 1
        assert errors[0].startswith("Component 'api.c001.example': invalid port")
        assert bad in errors[0]

    def test_only_the_first_bad_component_port_is_reported(self) -> None:
        """Parsing stops at the first bad port of a component."""
        errors = _errors(components=[_component("api.c001.example", ["bad-1", "bad-2"])])

        assert len(errors) == 1
        assert "bad-1" in errors[0]

    def test_component_without_ports_is_valid(self) -> None:
        """Ports are optional on a component."""
        assert _errors(components=[_component("worker.c001.example", None)]) == []


# ===========================================================================
# Source / target shape
# ===========================================================================


class TestSourceAndTargetShape:
    def test_both_source_and_source_profile_is_reported(self) -> None:
        """A dependency may not have a component source and an access-profile source."""
        errors = _errors(_dependency(source_profile=True, target=DB))

        assert errors == ["Dependency 'backend-to-db' must set exactly one of source or source_profile."]

    def test_neither_source_nor_source_profile_is_reported(self) -> None:
        """A dependency must come from somewhere."""
        errors = _errors(_dependency(source=None, target=DB))

        assert errors == ["Dependency 'backend-to-db' must set exactly one of source or source_profile."]

    def test_both_target_and_target_fqdn_is_reported(self) -> None:
        """A dependency may not target a component and an external fqdn at once."""
        errors = _errors(_dependency(ports=["tcp/9999"], target=DB, target_fqdn="api.stripe.com"))

        # The shape error stops validation: the uncovered port is not reported too.
        assert errors == ["Dependency 'backend-to-db' must set exactly one of target or target_fqdn."]

    def test_neither_target_nor_target_fqdn_is_reported(self) -> None:
        """A dependency must go somewhere."""
        errors = _errors(_dependency())

        assert errors == ["Dependency 'backend-to-db' must set exactly one of target or target_fqdn."]

    def test_blank_target_fqdn_counts_as_unset(self) -> None:
        """Whitespace is not an fqdn."""
        errors = _errors(_dependency(ports=["tcp/443"], target_fqdn="   "))

        assert errors == ["Dependency 'backend-to-db' must set exactly one of target or target_fqdn."]

    def test_source_and_target_errors_are_both_reported(self) -> None:
        """A dependency with no source and no target reports both."""
        errors = _errors(_dependency(source=None))

        assert errors == [
            "Dependency 'backend-to-db' must set exactly one of source or source_profile.",
            "Dependency 'backend-to-db' must set exactly one of target or target_fqdn.",
        ]

    def test_unnamed_dependency_uses_a_placeholder_label(self) -> None:
        """A dependency without a name is still identifiable in the message."""
        dep = _dependency()
        dep.pop("name")

        errors = _errors(dep)

        assert errors == ["Dependency '<unnamed>' must set exactly one of target or target_fqdn."]


# ===========================================================================
# Dependency ports
# ===========================================================================


class TestDependencyPorts:
    @pytest.mark.parametrize("bad", ["icmp/1", "tcp/0", "tcp/65536", "tcp/9000-8000", "tcp/", "garbage"])
    def test_malformed_dependency_port_is_reported_once(self, bad: str) -> None:
        """A bad dependency port is reported and stops further checks on it."""
        errors = _errors(_dependency(ports=[bad], target=DB))

        assert len(errors) == 1
        assert errors[0].startswith("Dependency 'backend-to-db': invalid port")
        assert bad in errors[0]

    def test_malformed_port_on_external_dependency_is_reported(self) -> None:
        """target_fqdn dependencies get the same port parsing."""
        errors = _errors(_dependency(ports=["tcp/443", "http"], target_fqdn="api.stripe.com"))

        assert len(errors) == 1
        assert errors[0].startswith("Dependency 'backend-to-db': invalid port 'http'")


# ===========================================================================
# External (target_fqdn) dependencies
# ===========================================================================


class TestExternalTarget:
    def test_external_target_without_ports_is_reported(self) -> None:
        """An external host has no ports of its own, so the dependency must list them."""
        errors = _errors(_dependency(target_fqdn="api.stripe.com"))

        assert errors == ["Dependency 'backend-to-db' targets api.stripe.com and must list its ports."]

    def test_external_target_from_an_access_profile_is_reported(self) -> None:
        """Access-profile grants publish a component; they cannot point at an external fqdn."""
        errors = _errors(_dependency(ports=["tcp/443"], target_fqdn="api.stripe.com", source=None, source_profile=True))

        assert errors == [
            "Dependency 'backend-to-db' grants an access profile an external fqdn; grants target a component."
        ]

    def test_external_target_without_owner_egress_service_is_reported(self) -> None:
        """No egress proxy on the source's owner: there is no path to the external host."""
        errors = _errors(_dependency(ports=["tcp/443"], target_fqdn="api.stripe.com", source=_source(egress=False)))

        assert errors == [
            "Dependency 'backend-to-db' targets api.stripe.com, but owner 'c001-customer' has no "
            "egress_service to reach it through."
        ]

    def test_external_target_with_source_lacking_an_owner_is_reported(self) -> None:
        """A source whose application has no owner has no egress service either."""
        errors = _errors(_dependency(ports=["tcp/443"], target_fqdn="api.stripe.com", source=_source(owner=False)))

        assert errors == [
            "Dependency 'backend-to-db' targets api.stripe.com, but owner '<unknown>' has no "
            "egress_service to reach it through."
        ]

    def test_external_target_with_ports_and_egress_is_valid(self) -> None:
        """Ports listed, owner egress present: no error."""
        assert _errors(_dependency(ports=["tcp/443", "udp/443"], target_fqdn="api.stripe.com")) == []

    def test_external_target_without_ports_and_egress_reports_both(self) -> None:
        """Missing ports and a missing egress service are independent errors."""
        errors = _errors(_dependency(target_fqdn="api.stripe.com", source=_source(egress=False)))

        assert len(errors) == 2
        assert "must list its ports" in errors[0]
        assert "has no egress_service" in errors[1]


# ===========================================================================
# Component targets
# ===========================================================================


class TestComponentTarget:
    def test_target_and_dependency_both_without_ports_is_reported(self) -> None:
        """Nothing to open on either side: the generator would skip it, so the check reports it."""
        errors = _errors(_dependency(target=_component("worker.c001.example", None)))

        assert errors == [
            "Dependency 'backend-to-db' lists no ports and its target 'worker.c001.example' declares none."
        ]

    def test_malformed_target_port_is_reported_once_on_the_component(self) -> None:
        """A bad target port is the component's error alone; its dependencies add neither a repeat nor coverage errors."""
        api = _component("api.c001.example", ["tcp/http"])
        errors = _errors(
            _dependency(ports=["tcp/1"], target=api),
            _dependency(name="frontend-to-api", ports=["tcp/2"], target=api),
            components=[api],
        )

        assert len(errors) == 1
        assert errors[0].startswith("Component 'api.c001.example': invalid port 'tcp/http'")

    def test_dependency_port_not_listed_by_the_target_is_reported(self) -> None:
        """A port the target does not listen on would open a hole to nothing."""
        errors = _errors(_dependency(ports=["tcp/5433"], target=DB))

        assert errors == [
            "Dependency 'backend-to-db' opens tcp/5433, which target 'db.c001.example' does not list in its ports."
        ]

    def test_protocol_mismatch_is_reported(self) -> None:
        """The same number on another protocol is not covered."""
        errors = _errors(_dependency(ports=["udp/5432"], target=DB))

        assert len(errors) == 1
        assert "opens udp/5432" in errors[0]

    @pytest.mark.parametrize(
        "port",
        ["udp/29999-30005", "udp/30005-30011", "udp/29000-31000"],
        ids=["starts-below", "ends-above", "wraps-around"],
    )
    def test_range_reaching_outside_the_listened_range_is_reported(self, port: str) -> None:
        """A sub-range is only covered when it lies wholly inside a listened range."""
        errors = _errors(_dependency(ports=[port], target=CACHE))

        assert len(errors) == 1
        assert f"opens {port}" in errors[0]

    def test_range_against_a_single_listened_port_is_reported(self) -> None:
        """A range is not covered by a single port at its start."""
        errors = _errors(_dependency(ports=["tcp/6379-6380"], target=CACHE))

        assert len(errors) == 1
        assert "opens tcp/6379-6380" in errors[0]

    @pytest.mark.parametrize(
        "port",
        ["tcp/6379", "udp/30000", "udp/30010", "udp/30000-30010", "udp/30001-30009"],
        ids=["single", "range-start", "range-end", "whole-range", "sub-range"],
    )
    def test_ports_inside_the_target_ports_are_valid(self, port: str) -> None:
        """Every port and sub-range the target listens on is covered."""
        assert _errors(_dependency(ports=[port], target=CACHE)) == []

    def test_each_uncovered_port_is_reported(self) -> None:
        """Covered ports pass; every uncovered one gets its own error."""
        errors = _errors(_dependency(ports=["tcp/5432", "tcp/22", "tcp/3389"], target=DB))

        assert len(errors) == 2
        assert "opens tcp/22" in errors[0]
        assert "opens tcp/3389" in errors[1]

    def test_access_profile_grant_ports_are_checked_against_the_target(self) -> None:
        """A grant may only publish ports the component actually listens on."""
        errors = _errors(_dependency(ports=["tcp/22"], source=None, source_profile=True, target=DB))

        assert len(errors) == 1
        assert "opens tcp/22" in errors[0]

    def test_errors_across_dependencies_are_all_reported(self) -> None:
        """One bad dependency does not hide another."""
        errors = _errors(
            _dependency("first", ports=["tcp/1"], target=DB),
            _dependency("second", target_fqdn="api.stripe.com"),
            _dependency("third", ports=["tcp/5432"], target=DB),
        )

        assert len(errors) == 2
        assert errors[0].startswith("Dependency 'first' opens tcp/1")
        assert errors[1].startswith("Dependency 'second' targets api.stripe.com")
