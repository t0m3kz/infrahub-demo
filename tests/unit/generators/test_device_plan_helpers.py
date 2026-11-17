"""The pure helpers a plan builder shares with create_devices and the border-services cabling."""

from __future__ import annotations

import asyncio
from typing import Any
from unittest.mock import AsyncMock, MagicMock

from generators.cloud.cloud_attachment_range import CloudAttachmentRangeGenerator
from generators.cloud.cloud_range import ensure_cloud_ranges
from generators.customers.customer_cloud import CustomerDeploymentCloudGenerator
from generators.dcim.cabling_plan import border_service_chains
from generators.dcim.device_plan import DeviceOptions, planned_device_names
from tests.unit.cloud_range_helpers import CloudClient, attachment, footprint, network
from tests.unit.segment_customer_pool_helpers import make_gen

_BORDER_ROLES = {"internal-firewall": "firewall", "external-firewall": "firewall", "load-balancer": "load-balancer"}


class TestPlannedDeviceNames:
    def test_names_are_sorted_flat_names_from_index_one(self) -> None:
        names = planned_device_names(
            fabric_name="fr2",
            pod_name=None,
            device_role="edge",
            quantity=2,
            naming_convention="standard",
            options=DeviceOptions(indexes=[]),
        )

        assert names == ["eg-fr201", "eg-fr202"]

    def test_role_index_start_continues_the_numbering(self) -> None:
        names = planned_device_names(
            fabric_name="fr2",
            pod_name=None,
            device_role="edge",
            quantity=2,
            naming_convention="standard",
            options=DeviceOptions(indexes=[], role_index_start=3),
        )

        assert names == ["eg-fr203", "eg-fr204"]

    def test_zero_devices_have_no_names(self) -> None:
        assert (
            planned_device_names(
                fabric_name="fr2",
                pod_name=None,
                device_role="edge",
                quantity=0,
                naming_convention="standard",
                options=DeviceOptions(indexes=[]),
            )
            == []
        )


class TestBorderServiceChains:
    def test_pbr_is_two_independent_legs(self) -> None:
        chains = border_service_chains(
            border_role_for=_BORDER_ROLES,
            connectivity_mode="pbr",
            border_names=["b1"],
            firewall_names=["f1"],
            load_balancer_names=["l1"],
        )

        assert chains == [
            [{"devices": ["b1"], "down_role": "firewall"}, {"devices": ["f1"], "up_role": "uplink"}],
            [{"devices": ["b1"], "down_role": "load-balancer"}, {"devices": ["l1"], "up_role": "uplink"}],
        ]

    def test_inline_chains_the_border_through_the_firewall_and_the_load_balancer_and_back(self) -> None:
        chains = border_service_chains(
            border_role_for=_BORDER_ROLES,
            connectivity_mode="inline",
            border_names=["b1"],
            firewall_names=["f1"],
            load_balancer_names=["l1"],
        )

        assert chains == [
            [{"devices": ["b1"], "down_role": "firewall"}, {"devices": ["f1"], "up_role": "uplink"}],
            [{"devices": ["f1"], "down_role": "downlink"}, {"devices": ["l1"], "up_role": "uplink"}],
            [{"devices": ["b1"], "down_role": "load-balancer"}, {"devices": ["l1"], "up_role": "downlink"}],
        ]


class TestAttachmentRootTracking:
    @staticmethod
    def _generator(footprints: list[Any]) -> Any:
        gen: Any = CloudAttachmentRangeGenerator.__new__(CloudAttachmentRangeGenerator)
        gen.logger = MagicMock()
        gen.client = MagicMock()
        gen.client.filters = AsyncMock(return_value=footprints)
        gen.client.group_context.related_node_ids = []
        return gen

    @staticmethod
    def _data(network_node: Any) -> dict[str, Any]:
        return {
            "CloudGatewayAttachment": {
                "edges": [{"node": {"id": "a1", "name": {"value": "tgwa"}, "virtual_network": {"node": network_node}}}]
            }
        }

    def test_an_attachment_to_a_gateway_is_still_the_runs_own_tracked_object(self) -> None:
        gen = self._generator([])

        asyncio.run(gen.generate(self._data(None)))

        assert gen.client.group_context.related_node_ids == ["a1"]

    def test_an_account_without_a_footprint_is_still_tracked_and_the_footprint_is_not(self) -> None:
        gen = self._generator([])
        node = {"id": "n1", "name": {"value": "vpc-a"}, "account": {"node": {"id": "acc-1", "name": {"value": "x"}}}}

        asyncio.run(gen.generate(self._data(node)))

        assert gen.client.group_context.related_node_ids == ["a1"]

    def test_a_response_without_an_id_tracks_nothing(self) -> None:
        gen = self._generator([])

        asyncio.run(gen.generate({"CloudGatewayAttachment": {"edges": [{"node": {"id": "", "name": {"value": "x"}}}]}}))

        gen.logger.error.assert_called_once()
        assert gen.client.group_context.related_node_ids == []


class TestEnsureCloudRangesRoot:
    def test_the_footprint_is_the_root_by_default(self) -> None:
        client = CloudClient([network("n1", "vpc-a")], [attachment("a1", "n1")])
        gen = make_gen(client, CustomerDeploymentCloudGenerator)

        asyncio.run(ensure_cloud_ranges(gen, footprint()))

        assert client.group_context.related_node_ids == ["cloud-1"]

    def test_a_given_root_is_tracked_instead_of_the_footprint(self) -> None:
        client = CloudClient([network("n1", "vpc-a")], [attachment("a1", "n1")])
        gen = make_gen(client, CustomerDeploymentCloudGenerator)

        asyncio.run(ensure_cloud_ranges(gen, footprint(), root_id="a1"))

        assert client.group_context.related_node_ids == ["a1"]
