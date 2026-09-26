"""Unit tests for InterconnectRequestGenerator (generators/topology/interconnect.py).

Covers connection_kind=virtual (fully automated, reconciled every run) and
connection_kind=physical_stub (planned stub, create-once, count-anchored
idempotency) dispatch from a TopologyInterconnectRequest.
"""

from __future__ import annotations

import asyncio
from typing import Any
from unittest.mock import AsyncMock, MagicMock

from generators.topology.interconnect import InterconnectRequestGenerator

_LOCATION_A = {"id": "loc-a", "name": "DC1", "__typename": "TopologyDataCenter"}
_LOCATION_B = {"id": "loc-b", "name": "eu-central-1", "__typename": "TopologyCloudRegion"}
_PROVIDER = {"id": "prov-1", "name": "Equinix"}


def _make_gen() -> Any:
    gen = InterconnectRequestGenerator.__new__(InterconnectRequestGenerator)
    gen.client = AsyncMock()
    gen.logger = MagicMock()
    return gen


def _iface(device_name: str, iface_name: str, iface_id: str) -> dict[str, Any]:
    return {"id": iface_id, "name": iface_name, "device": {"id": f"dev-{device_name}", "name": device_name}}


def _request(
    *,
    connection_kind: str,
    link_type: str | None = None,
    circuit_type: str | None = None,
    redundancy_count: int = 1,
    endpoint_a_interfaces: list[dict[str, Any]] | None = None,
    endpoint_b_interfaces: list[dict[str, Any]] | None = None,
    resulting_circuits: list[dict[str, Any]] | None = None,
    owner: dict[str, Any] | None = None,
    name: str = "dc1-eu-central-1",
) -> dict[str, Any]:
    return {
        "TopologyInterconnectRequest": [
            {
                "id": "req-1",
                "name": name,
                "description": None,
                "connection_kind": connection_kind,
                "link_type": link_type,
                "circuit_type": circuit_type,
                "redundancy_count": redundancy_count,
                "bandwidth": 1000,
                "location_a": _LOCATION_A,
                "location_b": _LOCATION_B,
                "provider": _PROVIDER,
                "owner": owner,
                "endpoint_a_interfaces": endpoint_a_interfaces or [],
                "endpoint_b_interfaces": endpoint_b_interfaces or [],
                "resulting_circuits": resulting_circuits or [],
            }
        ]
    }


# ===========================================================================
# TestVirtualDispatch
# ===========================================================================


class TestVirtualDispatch:
    def test_happy_path_creates_virtual_circuit(self) -> None:
        gen = _make_gen()
        created = MagicMock()
        created.save = AsyncMock()
        gen.client.create = AsyncMock(return_value=created)

        data = _request(
            connection_kind="virtual",
            link_type="direct_connect_aws",
            endpoint_a_interfaces=[_iface("edge1", "Gi0/1", "ifa-1")],
            endpoint_b_interfaces=[_iface("aws-dx", "eth0", "ifb-1")],
        )

        asyncio.run(gen.generate(data))

        gen.client.create.assert_awaited_once()
        call_kwargs = gen.client.create.call_args.kwargs
        payload = call_kwargs["data"]
        assert payload["name"] == "dc1-eu-central-1-01"
        assert payload["link_type"] == "direct_connect_aws"
        assert payload["locations"] == [{"id": "loc-a"}, {"id": "loc-b"}]
        assert payload["interface_capabilities"] == [{"id": "ifa-1"}, {"id": "ifb-1"}]
        assert payload["provider"] == {"id": "prov-1"}
        created.save.assert_awaited_once_with(allow_upsert=True)

    def test_idempotent_rerun_upserts_same_name(self) -> None:
        gen = _make_gen()
        created = MagicMock()
        created.save = AsyncMock()
        gen.client.create = AsyncMock(return_value=created)

        data = _request(
            connection_kind="virtual",
            link_type="sd_wan",
            endpoint_a_interfaces=[_iface("edge1", "Gi0/1", "ifa-1")],
            endpoint_b_interfaces=[_iface("hub1", "eth0", "ifb-1")],
        )

        asyncio.run(gen.generate(data))
        asyncio.run(gen.generate(data))

        assert gen.client.create.await_count == 2
        first_name = gen.client.create.await_args_list[0].kwargs["data"]["name"]
        second_name = gen.client.create.await_args_list[1].kwargs["data"]["name"]
        assert first_name == second_name

    def test_redundancy_fanout_creates_n_legs(self) -> None:
        gen = _make_gen()
        created = MagicMock()
        created.save = AsyncMock()
        gen.client.create = AsyncMock(return_value=created)

        data = _request(
            connection_kind="virtual",
            link_type="sd_wan",
            redundancy_count=2,
            endpoint_a_interfaces=[_iface("edge1", "Gi0/1", "ifa-1"), _iface("edge1", "Gi0/2", "ifa-2")],
            endpoint_b_interfaces=[_iface("hub1", "eth0", "ifb-1"), _iface("hub1", "eth1", "ifb-2")],
        )

        asyncio.run(gen.generate(data))

        assert gen.client.create.await_count == 2
        names = sorted(c.kwargs["data"]["name"] for c in gen.client.create.await_args_list)
        assert names == ["dc1-eu-central-1-01", "dc1-eu-central-1-02"]

    def test_missing_interfaces_skips_and_warns(self) -> None:
        gen = _make_gen()
        gen.client.create = AsyncMock()

        data = _request(connection_kind="virtual", link_type="sd_wan")

        asyncio.run(gen.generate(data))

        gen.client.create.assert_not_awaited()
        gen.logger.warning.assert_called_once()

    def test_partial_interface_shortfall_builds_available_and_warns(self) -> None:
        gen = _make_gen()
        created = MagicMock()
        created.save = AsyncMock()
        gen.client.create = AsyncMock(return_value=created)

        data = _request(
            connection_kind="virtual",
            link_type="sd_wan",
            redundancy_count=2,
            endpoint_a_interfaces=[_iface("edge1", "Gi0/1", "ifa-1")],
            endpoint_b_interfaces=[_iface("hub1", "eth0", "ifb-1")],
        )

        asyncio.run(gen.generate(data))

        assert gen.client.create.await_count == 1
        gen.logger.warning.assert_called_once()

    def test_missing_link_type_logs_error_and_skips(self) -> None:
        gen = _make_gen()
        gen.client.create = AsyncMock()

        data = _request(
            connection_kind="virtual",
            endpoint_a_interfaces=[_iface("edge1", "Gi0/1", "ifa-1")],
            endpoint_b_interfaces=[_iface("hub1", "eth0", "ifb-1")],
        )

        asyncio.run(gen.generate(data))

        gen.client.create.assert_not_awaited()
        gen.logger.error.assert_called_once()

    def test_vni_allocated_only_for_vxlan(self) -> None:
        gen = _make_gen()
        pool = MagicMock(id="pool-vni")
        gen.client.get = AsyncMock(return_value=pool)
        created = MagicMock()
        created.save = AsyncMock()
        gen.client.create = AsyncMock(return_value=created)

        data = _request(
            connection_kind="virtual",
            link_type="vxlan",
            endpoint_a_interfaces=[_iface("edge1", "Gi0/1", "ifa-1")],
            endpoint_b_interfaces=[_iface("hub1", "eth0", "ifb-1")],
        )

        asyncio.run(gen.generate(data))

        payload = gen.client.create.call_args.kwargs["data"]
        assert payload["vni"] == {"from_pool": {"id": "pool-vni"}, "identifier": "dc1-eu-central-1-01-vni"}
        assert "tunnel_id" not in payload

    def test_gre_key_allocated_only_for_gre(self) -> None:
        gen = _make_gen()
        pool = MagicMock(id="pool-gre")
        gen.client.get = AsyncMock(return_value=pool)
        created = MagicMock()
        created.save = AsyncMock()
        gen.client.create = AsyncMock(return_value=created)

        data = _request(
            connection_kind="virtual",
            link_type="gre",
            endpoint_a_interfaces=[_iface("edge1", "Gi0/1", "ifa-1")],
            endpoint_b_interfaces=[_iface("hub1", "eth0", "ifb-1")],
        )

        asyncio.run(gen.generate(data))

        payload = gen.client.create.call_args.kwargs["data"]
        assert payload["tunnel_id"] == {"from_pool": {"id": "pool-gre"}, "identifier": "dc1-eu-central-1-01-tunnel_id"}
        assert "vni" not in payload

    def test_no_pool_allocation_for_vpn_ipsec(self) -> None:
        gen = _make_gen()
        gen.client.get = AsyncMock()
        created = MagicMock()
        created.save = AsyncMock()
        gen.client.create = AsyncMock(return_value=created)

        data = _request(
            connection_kind="virtual",
            link_type="vpn_ipsec",
            endpoint_a_interfaces=[_iface("edge1", "Gi0/1", "ifa-1")],
            endpoint_b_interfaces=[_iface("hub1", "eth0", "ifb-1")],
        )

        asyncio.run(gen.generate(data))

        gen.client.get.assert_not_awaited()
        payload = gen.client.create.call_args.kwargs["data"]
        assert "vni" not in payload
        assert "tunnel_id" not in payload


# ===========================================================================
# TestPhysicalStubDispatch
# ===========================================================================


class TestPhysicalStubDispatch:
    def test_creates_planned_stub_without_interfaces(self) -> None:
        gen = _make_gen()
        stub = MagicMock(id="stub-1")
        stub.save = AsyncMock()
        request_obj = MagicMock()
        request_obj.save = AsyncMock()
        gen.client.create = AsyncMock(side_effect=[stub, request_obj])

        data = _request(connection_kind="physical_stub", circuit_type="dark_fiber")

        asyncio.run(gen.generate(data))

        first_call = gen.client.create.await_args_list[0]
        payload = first_call.kwargs["data"]
        assert payload["status"] == "provisioning"
        assert payload["circuit_type"] == "dark_fiber"
        assert "customer_interfaces" not in payload
        assert "provider_interfaces" not in payload
        stub.save.assert_awaited_once_with(allow_upsert=True, update_group_context=False)

    def test_idempotent_via_resulting_circuits_count_not_circuit_id(self) -> None:
        """Simulates ops having renamed the placeholder circuit_id to a real
        one once the provider order landed — the generator must not
        re-derive the old placeholder and create a duplicate."""
        gen = _make_gen()
        gen.client.create = AsyncMock()

        data = _request(
            connection_kind="physical_stub",
            circuit_type="dark_fiber",
            resulting_circuits=[
                {"id": "stub-1", "__typename": "TopologyPhysicalCircuit", "circuit_id": "REAL-CID-001"}
            ],
        )

        asyncio.run(gen.generate(data))

        gen.client.create.assert_not_awaited()

    def test_shortfall_creates_only_the_delta(self) -> None:
        gen = _make_gen()
        stub = MagicMock(id="stub-2")
        stub.save = AsyncMock()
        request_obj = MagicMock()
        request_obj.save = AsyncMock()
        gen.client.create = AsyncMock(side_effect=[stub, stub, request_obj])

        data = _request(
            connection_kind="physical_stub",
            circuit_type="dark_fiber",
            redundancy_count=3,
            resulting_circuits=[{"id": "stub-1", "__typename": "TopologyPhysicalCircuit", "circuit_id": "CID-01"}],
        )

        asyncio.run(gen.generate(data))

        # 2 new stubs + 1 request update = 3 create() calls
        assert gen.client.create.await_count == 3

    def test_redundancy_decrease_does_not_delete_existing_stub(self) -> None:
        gen = _make_gen()
        gen.client.create = AsyncMock()
        gen.client.delete = AsyncMock()

        data = _request(
            connection_kind="physical_stub",
            circuit_type="dark_fiber",
            redundancy_count=1,
            resulting_circuits=[
                {"id": "stub-1", "__typename": "TopologyPhysicalCircuit", "circuit_id": "CID-01"},
                {"id": "stub-2", "__typename": "TopologyPhysicalCircuit", "circuit_id": "CID-02"},
            ],
        )

        asyncio.run(gen.generate(data))

        gen.client.create.assert_not_awaited()
        gen.client.delete.assert_not_awaited()
        gen.logger.warning.assert_called_once()

    def test_missing_circuit_type_logs_error_and_skips(self) -> None:
        gen = _make_gen()
        gen.client.create = AsyncMock()

        data = _request(connection_kind="physical_stub")

        asyncio.run(gen.generate(data))

        gen.client.create.assert_not_awaited()
        gen.logger.error.assert_called_once()


# ===========================================================================
# TestUnknownConnectionKind
# ===========================================================================


class TestUnknownConnectionKind:
    def test_unknown_connection_kind_logs_error(self) -> None:
        gen = _make_gen()
        gen.client.create = AsyncMock()

        data = _request(connection_kind="bogus")

        asyncio.run(gen.generate(data))

        gen.client.create.assert_not_awaited()
        gen.logger.error.assert_called_once()
