"""Unit tests for approved application deployment request materialization."""

from __future__ import annotations

import asyncio
from typing import Any
from unittest.mock import AsyncMock, MagicMock

from generators.topology.application_request import AppDeploymentRequestGenerator


def _make_generator() -> Any:
    generator = AppDeploymentRequestGenerator.__new__(AppDeploymentRequestGenerator)
    generator.client = AsyncMock()
    generator.logger = MagicMock()
    return generator


def _request(status: str = "approved") -> dict[str, Any]:
    return {
        "id": "request-1",
        "name": "checkout",
        "status": status,
        "application_fqdn": "checkout.example.com",
        "environment": "production",
        "criticality": "high",
        "security_profile": "internet_exposed",
        "customer": {"id": "customer-source"},
        "components": [],
        "dependencies": [],
    }


class TestDeploymentRequestGenerator:
    def test_pending_request_does_not_create_catalogue_objects(self) -> None:
        generator = _make_generator()

        asyncio.run(generator.generate({"AppDeploymentRequest": [_request(status="pending")]}))

        generator.client.create.assert_not_called()

    def test_denied_request_does_not_create_catalogue_objects(self) -> None:
        generator = _make_generator()

        asyncio.run(generator.generate({"AppDeploymentRequest": [_request(status="denied")]}))

        generator.client.create.assert_not_called()

    def test_approved_request_creates_application(self) -> None:
        generator = _make_generator()
        application = MagicMock(id="application-1")
        application.save = AsyncMock()
        generator.client.filters = AsyncMock(return_value=[])
        generator.client.create = AsyncMock(return_value=application)

        asyncio.run(generator.generate({"AppDeploymentRequest": [_request()]}))

        generator.client.create.assert_awaited_once()
        payload = generator.client.create.call_args.kwargs["data"]
        assert payload["label"] == "checkout"
        assert payload["owner"] == {"id": "customer-source"}
        assert payload["fqdn"] == "checkout.example.com"

    def test_approved_external_dependency_creates_app_dependency(self) -> None:
        generator = _make_generator()
        source = MagicMock(id="component-1")
        target = MagicMock(id="endpoint-1")
        materialized = MagicMock()
        materialized.save = AsyncMock()
        generator.client.filters = AsyncMock(side_effect=[[], []])
        generator.client.create = AsyncMock(return_value=materialized)

        dependency = {
            "name": "checkout-to-stripe",
            "access_status": "approved",
            "protocol": "tcp",
            "port_start": 443,
            "port_end": None,
            "source": {"id": "request-component-1"},
            "target": {"id": "request-endpoint-1"},
            "approved_by": "customer-source",
        }

        asyncio.run(
            generator._materialize_dependency(
                dependency=dependency,
                component_map={"request-component-1": source},
                endpoint_map={"request-endpoint-1": target},
                request_customer_id="customer-source",
            )
        )

        payload = generator.client.create.call_args.kwargs["data"]
        assert payload["source"] == {"id": "component-1"}
        assert payload["target"] == {"id": "endpoint-1"}
        assert payload["access_status"] == "approved"

    def test_pending_dependency_is_not_materialized(self) -> None:
        generator = _make_generator()
        generator.client.filters = AsyncMock(return_value=[])
        dependency = {"name": "pending-access", "access_status": "pending"}

        asyncio.run(
            generator._materialize_dependency(
                dependency=dependency,
                component_map={},
                endpoint_map={},
                request_customer_id="customer-source",
            )
        )

        generator.client.create.assert_not_called()

    def test_existing_target_requires_target_owner_approval(self) -> None:
        generator = _make_generator()
        source = MagicMock(id="component-1")
        dependency = {
            "name": "checkout-to-auth",
            "access_status": "approved",
            "source": {"id": "request-component-1"},
            "existing_target": {
                "id": "endpoint-auth",
                "parent": {
                    "parent": {
                        "parent": {"owner": {"id": "customer-target"}},
                    },
                },
            },
            "approved_by": {"id": "customer-source"},
        }

        asyncio.run(
            generator._materialize_dependency(
                dependency=dependency,
                component_map={"request-component-1": source},
                endpoint_map={},
                request_customer_id="customer-source",
            )
        )

        generator.client.create.assert_not_called()
        generator.logger.warning.assert_called_once()
