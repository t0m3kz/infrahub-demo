"""Unit tests for AppApplicationGenerator orchestration (generators/applications/application_security.py)."""

from __future__ import annotations

import asyncio
from typing import Any
from unittest.mock import AsyncMock, MagicMock

from generators.applications.application_security import AppApplicationGenerator
from tests.unit.app_application_generator_helpers import _backend, _dep, _frontend

# TestDependencyRuleGenerator / TestComponentRuleGenerator


class TestDependencyRuleGenerator:
    """add_app_dependency only fans out to add_app_application; it writes nothing."""

    def _gen(self) -> Any:
        gen = AppApplicationGenerator.__new__(AppApplicationGenerator)
        gen.client = AsyncMock()
        gen.logger = MagicMock()
        gen.run_generator = AsyncMock()
        gen._reconcile_application_rules = AsyncMock()
        return gen

    @staticmethod
    def _assert_wrote_nothing(gen: Any) -> None:
        """No rule reconciliation, no query, no node created in this run's group."""
        gen._reconcile_application_rules.assert_not_awaited()
        gen.client.execute_graphql.assert_not_awaited()
        gen.client.create.assert_not_awaited()
        gen.client.filters.assert_not_awaited()

    def test_dependency_fans_out_to_the_source_application(self) -> None:
        """The trigger payload only names the application."""
        gen = self._gen()

        dep_data = {
            "AppDependency": [
                {
                    "id": "dep-1",
                    "name": "fe-to-api",
                    "source": {
                        "id": "comp-fe",
                        "name": "frontend",
                        "parent": {"id": "app-1", "name": "myapp"},
                    },
                    "target": {"id": "comp-api", "parent": {"id": "app-1", "name": "myapp"}},
                }
            ]
        }

        asyncio.run(gen.generate(dep_data))

        gen.run_generator.assert_awaited_once_with("add_app_application", ["app-1"], wait=False)
        self._assert_wrote_nothing(gen)

    def test_target_fqdn_dependency_fans_out_to_the_source_application(self) -> None:
        """An external dependency has no target component; the source's application is triggered."""
        gen = self._gen()

        dep_data = {
            "AppDependency": [
                {
                    "id": "dep-1",
                    "name": "backend-to-stripe",
                    "source": {"id": "comp-be", "name": "backend", "parent": {"id": "app-1", "name": "myapp"}},
                    "target": None,
                    "target_fqdn": "api.stripe.com",
                }
            ]
        }

        asyncio.run(gen.generate(dep_data))

        gen.run_generator.assert_awaited_once_with("add_app_application", ["app-1"], wait=False)
        self._assert_wrote_nothing(gen)

    def test_dependency_generator_skips_when_source_missing(self) -> None:
        """A dependency with neither source nor target names no application."""
        gen = self._gen()

        asyncio.run(gen.generate({"AppDependency": [{"id": "dep-1", "name": "fe-to-api"}]}))

        gen.run_generator.assert_not_called()
        self._assert_wrote_nothing(gen)

    def test_dependency_without_target_and_target_fqdn_is_skipped(self) -> None:
        """A source with nowhere to go triggers nothing and says why."""
        gen = self._gen()
        dep_data = {
            "AppDependency": [
                {
                    "id": "dep-1",
                    "name": "fe-to-nowhere",
                    "source": {"id": "comp-fe", "parent": {"id": "app-1", "name": "myapp"}},
                    "target": None,
                    "target_fqdn": None,
                }
            ]
        }

        asyncio.run(gen.generate(dep_data))

        gen.run_generator.assert_not_called()
        gen.logger.warning.assert_called_once()
        assert "neither a target component nor a target_fqdn" in gen.logger.warning.call_args.args[0]

    def test_source_without_parent_application_is_skipped(self) -> None:
        """A source component outside any application has nothing to trigger."""
        gen = self._gen()
        dep_data = {
            "AppDependency": [
                {"id": "dep-1", "name": "x", "source": {"id": "comp-fe", "parent": None}, "target": {"id": "c"}}
            ]
        }

        asyncio.run(gen.generate(dep_data))

        gen.run_generator.assert_not_called()
        gen.logger.warning.assert_called_once()

    def test_access_profile_grant_fans_out_to_the_target_application(self) -> None:
        """A dependency from an access profile has no source component."""
        gen = self._gen()

        dep_data = {
            "AppDependency": [
                {
                    "id": "dep-1",
                    "name": "c001-checkout-web-private-access",
                    "source_profile": {"id": "profile-1", "name": "c001-private-access-standard"},
                    "target": {"id": "comp-fe", "parent": {"id": "app-checkout", "name": "c001-checkout-p"}},
                }
            ]
        }

        asyncio.run(gen.generate(dep_data))

        gen.run_generator.assert_awaited_once_with("add_app_application", ["app-checkout"], wait=False)
        self._assert_wrote_nothing(gen)

    def test_access_profile_grant_without_target_application_is_skipped(self) -> None:
        """A grant whose target component has no parent application cannot be reconciled."""
        gen = self._gen()
        dep_data = {
            "AppDependency": [
                {
                    "id": "dep-1",
                    "name": "grant",
                    "source_profile": {"id": "profile-1"},
                    "target": {"id": "comp-fe", "parent": None},
                }
            ]
        }

        asyncio.run(gen.generate(dep_data))

        gen.run_generator.assert_not_called()
        gen.logger.warning.assert_called_once()

    def test_dependency_without_any_source_is_skipped(self) -> None:
        """Neither source nor source_profile: nothing to reconcile."""
        gen = self._gen()

        dep_data = {"AppDependency": [{"id": "dep-1", "name": "orphan", "target": {"id": "comp-1"}}]}

        asyncio.run(gen.generate(dep_data))

        gen.run_generator.assert_not_called()


def _component_payload(*dependents: dict[str, Any]) -> dict[str, Any]:
    """app_component payload: component of app-b with the given inbound dependencies."""
    return {
        "AppComponent": [
            {
                "id": "comp-db",
                "fqdn": "db.c001-b-p.example",
                "parent": {"id": "app-b", "name": "c001-b-p"},
                "dependents": list(dependents),
            }
        ]
    }


def _caller(dep_id: str, app_id: str) -> dict[str, Any]:
    return {"id": dep_id, "source": {"id": f"comp-{app_id}", "parent": {"id": app_id, "name": f"name-{app_id}"}}}


class TestComponentRuleGenerator:
    """add_app_component only fans out: its own application and every caller."""

    def _gen(self) -> Any:
        gen = AppApplicationGenerator.__new__(AppApplicationGenerator)
        gen.client = AsyncMock()
        gen.logger = MagicMock()
        gen.run_generator = AsyncMock()
        gen._reconcile_application_rules = AsyncMock()
        return gen

    def test_component_fans_out_to_its_own_application(self) -> None:
        """The component's own application gets an add_app_application run; nothing is written here."""
        gen = self._gen()

        asyncio.run(gen.generate(_component_payload()))

        gen.run_generator.assert_awaited_once_with("add_app_application", ["app-b"], wait=False)
        gen._reconcile_application_rules.assert_not_awaited()
        gen.client.execute_graphql.assert_not_awaited()
        gen.client.create.assert_not_awaited()

    def test_calling_applications_are_fanned_out_once_each(self) -> None:
        """Every other application calling the component is in the same single fan-out."""
        gen = self._gen()
        payload = _component_payload(_caller("dep-1", "app-c"), _caller("dep-2", "app-a"), _caller("dep-3", "app-a"))

        asyncio.run(gen.generate(payload))

        gen.run_generator.assert_awaited_once_with("add_app_application", ["app-a", "app-b", "app-c"], wait=False)
        gen._reconcile_application_rules.assert_not_awaited()

    def test_own_application_and_access_grants_add_no_extra_application(self) -> None:
        """A call from inside the application and an access-profile grant add no other application."""
        gen = self._gen()
        grant = {"id": "dep-grant", "source": None, "source_profile": {"id": "profile-1"}}
        payload = _component_payload(_caller("dep-1", "app-b"), grant)

        asyncio.run(gen.generate(payload))

        gen.run_generator.assert_awaited_once_with("add_app_application", ["app-b"], wait=False)

    def test_component_without_parent_application_still_fans_out_to_callers(self) -> None:
        """No own application: only the callers (if any) are triggered."""
        gen = self._gen()
        payload = {
            "AppComponent": [
                {"id": "comp-db", "fqdn": "db.example", "parent": None, "dependents": [_caller("dep-1", "app-a")]}
            ]
        }

        asyncio.run(gen.generate(payload))

        gen.run_generator.assert_awaited_once_with("add_app_application", ["app-a"], wait=False)
        gen.logger.warning.assert_called_once()

    def test_component_without_parent_or_callers_is_skipped(self) -> None:
        """Nothing to trigger at all: no fan-out."""
        gen = self._gen()
        payload = {"AppComponent": [{"id": "comp-db", "fqdn": "db.example", "parent": None}]}

        asyncio.run(gen.generate(payload))

        gen.run_generator.assert_not_called()
        gen.logger.warning.assert_called_once()


# TestDependencyEdges


class TestDependencyEdges:
    """Edges are built from depends_on (or the trigger payload)."""

    def test_component_edges_keep_target_and_target_fqdn_dependencies(self) -> None:
        """A component target and an external fqdn are both edges."""
        backend = _backend()
        internal = _dep("fe-to-be", target=backend)
        external = _dep("fe-to-stripe", ports=["tcp/443"], target_fqdn="api.stripe.com")
        frontend = _frontend(internal, external)

        edges = AppApplicationGenerator._dependency_edges_from_components([frontend])

        assert edges == [(frontend, internal, backend), (frontend, external, {})]

    def test_component_edges_ignore_a_dependency_without_any_target(self) -> None:
        """No target and no target_fqdn: dropped before any rule is attempted."""
        frontend = _frontend(_dep("fe-to-nowhere"))

        assert AppApplicationGenerator._dependency_edges_from_components([frontend]) == []
