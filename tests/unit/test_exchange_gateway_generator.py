"""Unit tests for ExchangeGatewayGenerator (generators/topology/exchange_gateway.py).

Covers:
  - generate() dispatch across TopologyCustomerDC/Colocation/Cloud/Office
  - _get_or_create_namespace() — idempotent namespace provisioning
  - _exchange_via_route_leak() — DC path (fabric EVPN route-target leak)
  - _exchange_via_circuit() — Cloud/Office path (routed exchange over a circuit)
"""

from __future__ import annotations

import asyncio
from typing import Any
from unittest.mock import AsyncMock, MagicMock

from generators.topology.exchange_gateway import SHARED_SERVICES_NAMESPACE, ExchangeGatewayGenerator

# ---------------------------------------------------------------------------
# Shared harness
# ---------------------------------------------------------------------------


def _make_gen() -> Any:
    gen = ExchangeGatewayGenerator.__new__(ExchangeGatewayGenerator)
    gen.client = AsyncMock()
    gen.logger = MagicMock()
    return gen


def _mock_node(node_id: str, name: str) -> MagicMock:
    node = MagicMock()
    node.id = node_id
    node.name = MagicMock()
    node.name.value = name
    node.save = AsyncMock()
    rel = MagicMock()
    rel.fetch = AsyncMock()
    rel.peers = []
    rel.add = MagicMock()
    node.customer_deployments = rel
    return node


def _customer_payload(
    kind: str,
    *,
    customer_id: str = "cust-1",
    name: str = "C007-P-DC3",
    org_id: str = "C007",
    owner_name: str = "Orbisan Ltd.",
    environment: str = "p",
    parent: dict | None = None,
) -> dict:
    node: dict[str, Any] = {
        kind: {
            "edges": [
                {
                    "node": {
                        "id": customer_id,
                        "name": {"value": name},
                        "environment": {"value": environment},
                        "owner": {
                            "node": {"id": "owner-1", "org_id": {"value": org_id}, "name": {"value": owner_name}}
                        },  # noqa: E501
                    }
                }
            ]
        }
    }
    if parent is not None:
        node[kind]["edges"][0]["node"]["parent"] = {"node": parent}
    return node


def _empty_graphql_response() -> dict:
    return {
        "TopologyCustomerDC": {"edges": []},
        "TopologyCustomerColocation": {"edges": []},
        "TopologyCustomerCloud": {"edges": []},
        "TopologyCustomerOffice": {"edges": []},
    }


def _with_kind(kind: str, payload: dict) -> dict:
    base = _empty_graphql_response()
    base[kind] = payload[kind]
    return base


def _run(coro):
    return asyncio.run(coro)


def _two_interfaces() -> list[dict]:
    return [{"id": "iface-1"}, {"id": "iface-2"}]


# ===========================================================================
# generate() — dispatch
# ===========================================================================


class TestGenerateDispatch:
    def test_no_customer_data_logs_error_and_returns(self):
        gen = _make_gen()
        _run(gen.generate(_empty_graphql_response()))
        gen.logger.error.assert_called_once()
        gen.client.filters.assert_not_called()

    def test_missing_org_id_logs_error(self):
        gen = _make_gen()
        payload = _with_kind("TopologyCustomerDC", _customer_payload("TopologyCustomerDC", org_id=""))
        _run(gen.generate(payload))
        gen.logger.error.assert_called_once()

    def test_dc_deployment_dispatches_to_route_leak(self):
        gen = _make_gen()
        gen._get_or_create_namespace = AsyncMock(return_value=_mock_node("ns-1", "C007-P"))
        gen._exchange_via_route_leak = AsyncMock()
        gen._exchange_via_circuit = AsyncMock()

        payload = _with_kind("TopologyCustomerDC", _customer_payload("TopologyCustomerDC"))
        _run(gen.generate(payload))

        gen._exchange_via_route_leak.assert_called_once()
        gen._exchange_via_circuit.assert_not_called()

    def test_cloud_deployment_dispatches_to_circuit_exchange(self):
        gen = _make_gen()
        gen._get_or_create_namespace = AsyncMock(return_value=_mock_node("ns-1", "C003-P"))
        gen._exchange_via_route_leak = AsyncMock()
        gen._exchange_via_circuit = AsyncMock()

        payload = _with_kind("TopologyCustomerCloud", _customer_payload("TopologyCustomerCloud", org_id="C003"))
        _run(gen.generate(payload))

        gen._exchange_via_circuit.assert_called_once()
        gen._exchange_via_route_leak.assert_not_called()

    def test_colocation_deployment_only_provisions_namespace(self):
        gen = _make_gen()
        gen._get_or_create_namespace = AsyncMock(return_value=_mock_node("ns-1", "C002-P"))
        gen._exchange_via_route_leak = AsyncMock()
        gen._exchange_via_circuit = AsyncMock()

        payload = _with_kind(
            "TopologyCustomerColocation", _customer_payload("TopologyCustomerColocation", org_id="C002")
        )
        _run(gen.generate(payload))

        gen._exchange_via_route_leak.assert_not_called()
        gen._exchange_via_circuit.assert_not_called()
        gen.logger.warning.assert_called_once()

    def test_office_deployment_dispatches_to_circuit_exchange(self):
        gen = _make_gen()
        gen._get_or_create_namespace = AsyncMock(return_value=_mock_node("ns-1", "INTERNAL-P"))
        gen._exchange_via_route_leak = AsyncMock()
        gen._exchange_via_circuit = AsyncMock()

        payload = _with_kind(
            "TopologyCustomerOffice",
            _customer_payload("TopologyCustomerOffice", org_id="INTERNAL", owner_name="Infrahub Corp"),
        )
        _run(gen.generate(payload))

        gen._exchange_via_circuit.assert_called_once()
        gen._exchange_via_route_leak.assert_not_called()

    def test_namespace_name_derived_from_org_id_and_environment(self):
        gen = _make_gen()
        gen._get_or_create_namespace = AsyncMock(return_value=_mock_node("ns-1", "C007-P"))
        gen._exchange_via_route_leak = AsyncMock()

        payload = _with_kind(
            "TopologyCustomerDC", _customer_payload("TopologyCustomerDC", org_id="c007", environment="n")
        )
        _run(gen.generate(payload))

        gen._get_or_create_namespace.assert_called_once_with("C007-N", owner_name="Orbisan Ltd.")

    def test_namespace_creation_failure_skips_exchange(self):
        gen = _make_gen()
        gen._get_or_create_namespace = AsyncMock(return_value=None)
        gen._exchange_via_route_leak = AsyncMock()

        payload = _with_kind("TopologyCustomerDC", _customer_payload("TopologyCustomerDC"))
        _run(gen.generate(payload))

        gen._exchange_via_route_leak.assert_not_called()


# ===========================================================================
# _get_or_create_namespace()
# ===========================================================================


class TestGetOrCreateNamespace:
    def test_existing_namespace_returned_without_create(self):
        gen = _make_gen()
        existing = _mock_node("ns-1", "C007-P")
        gen.client.filters = AsyncMock(return_value=[existing])

        result = _run(gen._get_or_create_namespace("C007-P", owner_name="Orbisan Ltd."))

        assert result is existing
        gen.client.create.assert_not_called()

    def test_missing_vrf_group_returns_none(self):
        gen = _make_gen()
        gen.client.filters = AsyncMock(return_value=[])
        gen.client.get = AsyncMock(side_effect=Exception("not found"))

        result = _run(gen._get_or_create_namespace("C007-P", owner_name="Orbisan Ltd."))

        assert result is None
        gen.logger.error.assert_called_once()

    def test_new_namespace_created_with_vrf_group(self):
        gen = _make_gen()
        gen.client.filters = AsyncMock(return_value=[])
        vrf_group = _mock_node("group-1", "vrf_namespaces")
        gen.client.get = AsyncMock(return_value=vrf_group)
        new_ns = _mock_node("ns-1", "C007-P")
        gen.client.create = AsyncMock(return_value=new_ns)

        result = _run(gen._get_or_create_namespace("C007-P", owner_name="Orbisan Ltd."))

        assert result is new_ns
        call_kwargs = gen.client.create.call_args.kwargs
        assert call_kwargs["data"]["name"] == "C007-P"
        assert call_kwargs["data"]["member_of_groups"] == [{"id": "group-1"}]
        new_ns.save.assert_called_once_with(allow_upsert=True)

    def test_create_exception_returns_none(self):
        gen = _make_gen()
        gen.client.filters = AsyncMock(return_value=[])
        gen.client.get = AsyncMock(return_value=_mock_node("group-1", "vrf_namespaces"))
        gen.client.create = AsyncMock(side_effect=Exception("boom"))

        result = _run(gen._get_or_create_namespace("C007-P", owner_name="Orbisan Ltd."))

        assert result is None
        gen.logger.error.assert_called_once()


# ===========================================================================
# _exchange_via_route_leak() — DC path
# ===========================================================================


class TestExchangeViaRouteLeak:
    def test_shared_namespace_missing_logs_warning(self):
        gen = _make_gen()
        gen._get_namespace = AsyncMock(return_value=None)
        customer_ns = _mock_node("ns-1", "C007-P")

        _run(gen._exchange_via_route_leak(customer_ns, "cust-1"))

        gen.logger.warning.assert_called_once()
        gen.client.create.assert_not_called()

    def test_customer_is_shared_namespace_itself_skips(self):
        gen = _make_gen()
        shared_ns = _mock_node("ns-shared", SHARED_SERVICES_NAMESPACE)
        gen._get_namespace = AsyncMock(return_value=shared_ns)

        _run(gen._exchange_via_route_leak(shared_ns, "cust-1"))

        gen.client.create.assert_not_called()

    def test_existing_exchange_not_duplicated(self):
        gen = _make_gen()
        shared_ns = _mock_node("ns-shared", SHARED_SERVICES_NAMESPACE)
        gen._get_namespace = AsyncMock(return_value=shared_ns)
        existing_exchange = _mock_node("ex-1", "existing")
        gen.client.filters = AsyncMock(return_value=[existing_exchange])
        customer_ns = _mock_node("ns-1", "C007-P")

        _run(gen._exchange_via_route_leak(customer_ns, "cust-1"))

        gen.client.create.assert_not_called()

    def test_existing_exchange_links_customer_deployment(self):
        gen = _make_gen()
        shared_ns = _mock_node("ns-shared", SHARED_SERVICES_NAMESPACE)
        gen._get_namespace = AsyncMock(return_value=shared_ns)
        existing_exchange = _mock_node("ex-1", "existing")
        gen.client.filters = AsyncMock(return_value=[existing_exchange])
        customer_ns = _mock_node("ns-1", "C007-P")

        _run(gen._exchange_via_route_leak(customer_ns, "cust-1"))

        existing_exchange.customer_deployments.add.assert_called_once_with({"id": "cust-1"})
        existing_exchange.save.assert_called_once_with(allow_upsert=True)

    def test_existing_exchange_already_linked_skips_relink(self):
        gen = _make_gen()
        shared_ns = _mock_node("ns-shared", SHARED_SERVICES_NAMESPACE)
        gen._get_namespace = AsyncMock(return_value=shared_ns)
        existing_exchange = _mock_node("ex-1", "existing")
        existing_peer = MagicMock()
        existing_peer.id = "cust-1"
        existing_exchange.customer_deployments.peers = [existing_peer]
        gen.client.filters = AsyncMock(return_value=[existing_exchange])
        customer_ns = _mock_node("ns-1", "C007-P")

        _run(gen._exchange_via_route_leak(customer_ns, "cust-1"))

        existing_exchange.customer_deployments.add.assert_not_called()

    def test_new_exchange_created_bidirectional(self):
        gen = _make_gen()
        shared_ns = _mock_node("ns-shared", SHARED_SERVICES_NAMESPACE)
        gen._get_namespace = AsyncMock(return_value=shared_ns)
        gen.client.filters = AsyncMock(return_value=[])
        new_exchange = _mock_node("ex-1", "C007-P-SHARED-SERVICES-shared-services")
        gen.client.create = AsyncMock(return_value=new_exchange)
        customer_ns = _mock_node("ns-1", "C007-P")

        _run(gen._exchange_via_route_leak(customer_ns, "cust-1"))

        call_kwargs = gen.client.create.call_args.kwargs
        assert call_kwargs["kind"] == "TopologyRouteLeakExchange"
        assert call_kwargs["data"]["namespace_a"] == {"id": "ns-1"}
        assert call_kwargs["data"]["customer_deployments"] == [{"id": "cust-1"}]
        assert call_kwargs["data"]["namespace_z"] == {"id": "ns-shared"}
        assert call_kwargs["data"]["direction"] == "bidirectional"
        new_exchange.save.assert_called_once_with(allow_upsert=True)


# ===========================================================================
# _exchange_via_circuit() — Cloud path
# ===========================================================================


class TestExchangeViaCircuit:
    def test_no_circuits_logs_warning_and_skips(self):
        gen = _make_gen()
        customer_ns = _mock_node("ns-1", "C003-P")
        customer = {"parent": {"circuits": []}}

        _run(gen._exchange_via_circuit(customer_ns, customer, "C003-P", "cust-1"))

        gen.logger.warning.assert_called_once()
        gen.client.create.assert_not_called()

    def test_non_virtual_circuit_ignored(self):
        gen = _make_gen()
        customer_ns = _mock_node("ns-1", "C003-P")
        customer = {"parent": {"circuits": [{"typename": "TopologyPhysicalCircuit"}]}}

        _run(gen._exchange_via_circuit(customer_ns, customer, "C003-P", "cust-1"))

        gen.logger.warning.assert_called_once()
        gen.client.create.assert_not_called()

    def test_circuit_with_wrong_interface_count_skips(self):
        gen = _make_gen()
        customer_ns = _mock_node("ns-1", "C003-P")
        customer = {
            "parent": {
                "circuits": [
                    {
                        "typename": "TopologyVirtualCircuit",
                        "id": "circuit-1",
                        "name": "vc-1",
                        "interfaces": [{"id": "iface-1"}],
                    }
                ]
            }
        }

        _run(gen._exchange_via_circuit(customer_ns, customer, "C003-P", "cust-1"))

        gen.logger.warning.assert_called_once()
        gen.client.create.assert_not_called()

    def test_missing_shared_namespace_logs_warning(self):
        gen = _make_gen()
        gen._get_namespace = AsyncMock(return_value=None)
        customer_ns = _mock_node("ns-1", "C003-P")
        customer = {
            "parent": {
                "circuits": [
                    {
                        "typename": "TopologyVirtualCircuit",
                        "id": "circuit-1",
                        "name": "vc-1",
                        "customer_deployment": {"id": "cust-1"},
                        "interfaces": _two_interfaces(),
                    }
                ]
            }
        }

        _run(gen._exchange_via_circuit(customer_ns, customer, "C003-P", "cust-1"))

        gen.logger.warning.assert_called_once()
        gen.client.create.assert_not_called()

    def test_circuit_not_owned_by_customer_skips(self):
        """Two customers can share a physical location (Office/CloudRegion), each with its own
        circuit terminating there — a circuit whose customer_deployment is a DIFFERENT customer's
        footprint must never be picked, or namespace A's exchange gets wired onto namespace B's
        circuit interfaces (a cross-tenant leak)."""
        gen = _make_gen()
        customer_ns = _mock_node("ns-1", "C003-P")
        customer = {
            "parent": {
                "circuits": [
                    {
                        "typename": "TopologyVirtualCircuit",
                        "id": "circuit-1",
                        "name": "vc-1",
                        "customer_deployment": {"id": "some-other-customer"},
                        "interfaces": _two_interfaces(),
                    }
                ]
            }
        }

        _run(gen._exchange_via_circuit(customer_ns, customer, "C003-P", "cust-1"))

        gen.logger.warning.assert_called_once()
        gen.client.create.assert_not_called()

    def test_valid_circuit_creates_routed_exchange_on_circuit_interfaces(self):
        gen = _make_gen()
        shared_ns = _mock_node("ns-shared", SHARED_SERVICES_NAMESPACE)
        gen._get_namespace = AsyncMock(return_value=shared_ns)
        gen.client.filters = AsyncMock(return_value=[])
        new_exchange = _mock_node("ex-1", "C003-P-SHARED-SERVICES-shared-services")
        gen.client.create = AsyncMock(return_value=new_exchange)
        customer_ns = _mock_node("ns-1", "C003-P")
        customer = {
            "parent": {
                "circuits": [
                    {
                        "typename": "TopologyVirtualCircuit",
                        "id": "circuit-1",
                        "name": "vc-1",
                        "customer_deployment": {"id": "cust-1"},
                        "interfaces": _two_interfaces(),
                    }
                ]
            }
        }

        _run(gen._exchange_via_circuit(customer_ns, customer, "C003-P", "cust-1"))

        call_kwargs = gen.client.create.call_args.kwargs
        assert call_kwargs["kind"] == "TopologyRoutedExchange"
        assert call_kwargs["data"]["namespace_a"] == {"id": "ns-1"}
        assert call_kwargs["data"]["namespace_z"] == {"id": "ns-shared"}
        assert call_kwargs["data"]["interface_capabilities"] == [{"id": "iface-1"}, {"id": "iface-2"}]
        assert call_kwargs["data"]["customer_deployments"] == [{"id": "cust-1"}]
        new_exchange.save.assert_called_once_with(allow_upsert=True)

    def test_hub_region_namespace_used_as_z_side_instead_of_shared_services(self):
        """Circuit's other endpoint is a hub CloudRegion with its own namespace (e.g. an SD-WAN
        PoP) — the exchange must stop at the hub, not reach straight into SHARED-SERVICES."""
        gen = _make_gen()
        gen._get_namespace = AsyncMock()
        gen.client.filters = AsyncMock(return_value=[])
        new_exchange = _mock_node("ex-1", "C001-P-INTERNET-hub")
        gen.client.create = AsyncMock(return_value=new_exchange)
        office_ns = _mock_node("ns-1", "C001-P")
        office = {
            "parent": {
                "id": "office-loc-1",
                "circuits": [
                    {
                        "typename": "TopologyVirtualCircuit",
                        "id": "circuit-1",
                        "name": "vc-1",
                        "customer_deployment": {"id": "office-1"},
                        "interfaces": _two_interfaces(),
                        "locations": [
                            {"id": "office-loc-1", "typename": "TopologyOffice"},
                            {
                                "id": "hub-region-1",
                                "typename": "TopologyCloudRegion",
                                "namespace": {"id": "ns-internet", "name": "INTERNET"},
                            },
                        ],
                    }
                ],
            }
        }

        _run(gen._exchange_via_circuit(office_ns, office, "C001-P", "office-1"))

        gen._get_namespace.assert_not_called()
        call_kwargs = gen.client.create.call_args.kwargs
        assert call_kwargs["data"]["namespace_a"] == {"id": "ns-1"}
        assert call_kwargs["data"]["namespace_z"] == {"id": "ns-internet"}

    def test_hub_region_without_namespace_falls_back_to_shared_services(self):
        gen = _make_gen()
        shared_ns = _mock_node("ns-shared", SHARED_SERVICES_NAMESPACE)
        gen._get_namespace = AsyncMock(return_value=shared_ns)
        gen.client.filters = AsyncMock(return_value=[])
        new_exchange = _mock_node("ex-1", "C001-P-SHARED-SERVICES-shared-services")
        gen.client.create = AsyncMock(return_value=new_exchange)
        office_ns = _mock_node("ns-1", "C001-P")
        office = {
            "parent": {
                "id": "office-loc-1",
                "circuits": [
                    {
                        "typename": "TopologyVirtualCircuit",
                        "id": "circuit-1",
                        "name": "vc-1",
                        "customer_deployment": {"id": "office-1"},
                        "interfaces": _two_interfaces(),
                        "locations": [
                            {"id": "office-loc-1", "typename": "TopologyOffice"},
                            {"id": "dc-1", "typename": "TopologyDataCenter", "namespace": None},
                        ],
                    }
                ],
            }
        }

        _run(gen._exchange_via_circuit(office_ns, office, "C001-P", "office-1"))

        call_kwargs = gen.client.create.call_args.kwargs
        assert call_kwargs["data"]["namespace_z"] == {"id": "ns-shared"}

    def test_existing_routed_exchange_not_duplicated(self):
        gen = _make_gen()
        shared_ns = _mock_node("ns-shared", SHARED_SERVICES_NAMESPACE)
        gen._get_namespace = AsyncMock(return_value=shared_ns)
        existing_exchange = _mock_node("ex-1", "existing")
        gen.client.filters = AsyncMock(return_value=[existing_exchange])
        customer_ns = _mock_node("ns-1", "C003-P")
        customer = {
            "parent": {
                "circuits": [
                    {
                        "typename": "TopologyVirtualCircuit",
                        "id": "circuit-1",
                        "name": "vc-1",
                        "customer_deployment": {"id": "cust-1"},
                        "interfaces": _two_interfaces(),
                    }
                ]
            }
        }

        _run(gen._exchange_via_circuit(customer_ns, customer, "C003-P", "cust-1"))

        gen.client.create.assert_not_called()
        existing_exchange.customer_deployments.add.assert_called_once_with({"id": "cust-1"})

    def test_office_uses_same_circuit_path_as_cloud(self):
        """TopologyCustomerOffice mirrors TopologyCustomerCloud exactly — parent.circuits, no special-casing."""
        gen = _make_gen()
        shared_ns = _mock_node("ns-shared", SHARED_SERVICES_NAMESPACE)
        gen._get_namespace = AsyncMock(return_value=shared_ns)
        gen.client.filters = AsyncMock(return_value=[])
        new_exchange = _mock_node("ex-1", "INTERNAL-P-SHARED-SERVICES-shared-services")
        gen.client.create = AsyncMock(return_value=new_exchange)
        office_ns = _mock_node("ns-1", "INTERNAL-P")
        office = {
            "parent": {
                "circuits": [
                    {
                        "typename": "TopologyVirtualCircuit",
                        "id": "circuit-1",
                        "name": "vc-1",
                        "customer_deployment": {"id": "office-1"},
                        "interfaces": _two_interfaces(),
                    }
                ]
            }
        }

        _run(gen._exchange_via_circuit(office_ns, office, "INTERNAL-P", "office-1"))

        call_kwargs = gen.client.create.call_args.kwargs
        assert call_kwargs["data"]["namespace_a"] == {"id": "ns-1"}
        assert call_kwargs["data"]["interface_capabilities"] == [{"id": "iface-1"}, {"id": "iface-2"}]
