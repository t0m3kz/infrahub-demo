"""Unit tests for CloudSecurityRuleMixin (generators/cloud_security.py).

Covers:
  - _get_or_create_sg()   — async, with caching
  - _create_cloud_rule()  — async, composite helper
"""

from __future__ import annotations

import asyncio
from typing import Any
from unittest.mock import AsyncMock, MagicMock

from generators.cloud_security import CloudSecurityRuleMixin
from generators.protocols import CloudSecurityGroup, CloudSecurityGroupRule

# ---------------------------------------------------------------------------
# Shared harness
# ---------------------------------------------------------------------------


def _make_gen() -> Any:
    gen = CloudSecurityRuleMixin.__new__(CloudSecurityRuleMixin)
    gen.client = AsyncMock()
    gen.logger = MagicMock()
    return gen


def _mock_sg(sg_id: str = "sg-id-1", name: str = "sg-myapp") -> MagicMock:
    sg = MagicMock()
    sg.id = sg_id
    sg.name = MagicMock()
    sg.name.value = name
    sg.save = AsyncMock()
    return sg


def _dep(
    protocol: str | None = None,
    port_start: int | None = None,
    port_end: int | None = None,
    name: str = "dep-1",
) -> dict:
    return {
        "id": f"dep-{name}",
        "name": name,
        "protocol": protocol,
        "port_start": port_start,
        "port_end": port_end,
        "description": None,
    }


# ===========================================================================
# TestGetOrCreateSg
# ===========================================================================


class TestGetOrCreateSg:
    def test_existing_sg_found_returns_it(self):
        gen = _make_gen()
        existing_sg = _mock_sg()
        gen.client.filters = AsyncMock(return_value=[existing_sg])
        gen.client.create = AsyncMock()

        result = asyncio.run(gen._get_or_create_sg("sg-myapp", "vnet-1", None))

        assert result is existing_sg
        gen.client.create.assert_not_called()
        existing_sg.save.assert_called_once()

    def test_no_sg_creates_new(self):
        gen = _make_gen()
        gen.client.filters = AsyncMock(return_value=[])
        new_sg = _mock_sg()
        gen.client.create = AsyncMock(return_value=new_sg)

        result = asyncio.run(gen._get_or_create_sg("sg-myapp", "vnet-99", None))

        assert result is new_sg
        gen.client.create.assert_called_once()
        call_kwargs = gen.client.create.call_args.kwargs
        assert call_kwargs["kind"] == CloudSecurityGroup
        data = call_kwargs["data"]
        assert data["name"] == "sg-myapp"
        assert data["virtual_network"] == {"id": "vnet-99"}

    def test_create_includes_account_when_provided(self):
        gen = _make_gen()
        gen.client.filters = AsyncMock(return_value=[])
        new_sg = _mock_sg()
        gen.client.create = AsyncMock(return_value=new_sg)

        asyncio.run(gen._get_or_create_sg("sg-myapp", "vnet-1", "acct-1"))

        data = gen.client.create.call_args.kwargs["data"]
        assert data["account"] == {"id": "acct-1"}

    def test_create_omits_account_when_none(self):
        gen = _make_gen()
        gen.client.filters = AsyncMock(return_value=[])
        new_sg = _mock_sg()
        gen.client.create = AsyncMock(return_value=new_sg)

        asyncio.run(gen._get_or_create_sg("sg-myapp", "vnet-1", None))

        data = gen.client.create.call_args.kwargs["data"]
        assert "account" not in data

    def test_cache_hit_avoids_second_filters_call(self):
        gen = _make_gen()
        existing_sg = _mock_sg()
        gen.client.filters = AsyncMock(return_value=[existing_sg])

        result1 = asyncio.run(gen._get_or_create_sg("sg-myapp", "vnet-1", None))
        result2 = asyncio.run(gen._get_or_create_sg("sg-myapp", "vnet-1", None))

        assert result1 is existing_sg
        assert result2 is existing_sg
        gen.client.filters.assert_called_once()

    def test_create_exception_returns_none(self):
        gen = _make_gen()
        gen.client.filters = AsyncMock(return_value=[])
        gen.client.create = AsyncMock(side_effect=Exception("API error"))

        result = asyncio.run(gen._get_or_create_sg("sg-myapp", "vnet-1", None))

        assert result is None
        gen.logger.error.assert_called_once()


# ===========================================================================
# TestCreateCloudRule
# ===========================================================================


class TestCreateCloudRule:
    @staticmethod
    def _cloud_seg(vnet_id: str = "vnet-1", acct_id: str | None = None) -> dict:
        vnet: dict = {"id": vnet_id}
        if acct_id:
            vnet["account"] = {"id": acct_id}
        return {
            "typename": "CloudNetworkSegment",
            "id": "cloud-seg-1",
            "name": "cloud-seg",
            "virtual_network": vnet,
        }

    @staticmethod
    def _onprem_seg(cidr: str = "192.168.10.0/24") -> dict:
        return {
            "typename": "ManagedVxlanSegment",
            "id": "onprem-seg-1",
            "name": "onprem-seg",
            "prefix": [{"prefix": cidr}],
        }

    @staticmethod
    def _comp(seg: dict, name: str = "web", comp_type: str = "frontend") -> dict:
        return {"id": f"comp-{name}", "name": name, "component_type": comp_type, "network_segment": seg}

    def _make_gen_with_sg(self, sg: MagicMock | None = None) -> Any:
        gen = _make_gen()
        mock_sg = sg or _mock_sg()
        gen._get_or_create_sg = AsyncMock(return_value=mock_sg)
        gen.client.filters = AsyncMock(return_value=[])
        new_rule = MagicMock()
        new_rule.save = AsyncMock()
        gen.client.create = AsyncMock(return_value=new_rule)
        return gen

    # ------------------------------------------------------------------
    # Direction tests
    # ------------------------------------------------------------------

    def test_dst_cloud_segment_sets_ingress_direction(self):
        gen = self._make_gen_with_sg()
        src_comp = self._comp(self._onprem_seg(), "frontend", "frontend")
        dst_comp = self._comp(self._cloud_seg(), "api", "backend")
        dep = _dep(protocol="tcp", port_start=443)

        asyncio.run(gen._create_cloud_rule("myapp", src_comp, dst_comp, dep, "myapp-frontend-to-api"))

        rule_data = gen.client.create.call_args.kwargs["data"]
        assert rule_data["direction"] == "ingress"

    def test_src_cloud_segment_sets_egress_direction(self):
        gen = self._make_gen_with_sg()
        src_comp = self._comp(self._cloud_seg(), "api", "backend")
        dst_comp = self._comp(self._onprem_seg(), "db", "database")
        dep = _dep(protocol="tcp", port_start=5432)

        asyncio.run(gen._create_cloud_rule("myapp", src_comp, dst_comp, dep, "myapp-api-to-db"))

        rule_data = gen.client.create.call_args.kwargs["data"]
        assert rule_data["direction"] == "egress"

    # ------------------------------------------------------------------
    # Port-explicit tests
    # ------------------------------------------------------------------

    def test_explicit_port_used_in_cloud_rule(self):
        gen = self._make_gen_with_sg()
        src_comp = self._comp(self._onprem_seg(), "fe", "frontend")
        dst_comp = self._comp(self._cloud_seg(), "api", "backend")
        dep = _dep(protocol="tcp", port_start=8200)

        asyncio.run(gen._create_cloud_rule("myapp", src_comp, dst_comp, dep, "myapp-fe-to-api"))

        rule_data = gen.client.create.call_args.kwargs["data"]
        assert rule_data["protocol"] == "tcp"
        assert rule_data["port_start"] == 8200

    def test_port_range_set_in_cloud_rule(self):
        gen = self._make_gen_with_sg()
        src_comp = self._comp(self._onprem_seg(), "fe", "frontend")
        dst_comp = self._comp(self._cloud_seg(), "api", "backend")
        dep = _dep(protocol="tcp", port_start=8000, port_end=8080)

        asyncio.run(gen._create_cloud_rule("myapp", src_comp, dst_comp, dep, "myapp-fe-to-api"))

        rule_data = gen.client.create.call_args.kwargs["data"]
        assert rule_data["port_start"] == 8000
        assert rule_data["port_end"] == 8080

    def test_no_port_on_dep_returns_false(self):
        """A dependency without port info must be rejected — no fallback."""
        gen = self._make_gen_with_sg()
        src_comp = self._comp(self._onprem_seg(), "fe", "frontend")
        dst_comp = self._comp(self._cloud_seg(), "api", "backend")
        dep = _dep()  # no protocol, no port_start

        result = asyncio.run(gen._create_cloud_rule("myapp", src_comp, dst_comp, dep, "myapp-fe-to-api"))

        assert result is False
        gen.client.create.assert_not_called()
        gen.logger.warning.assert_called()

    # ------------------------------------------------------------------
    # Early-exit paths
    # ------------------------------------------------------------------

    def test_no_vnet_id_returns_false(self):
        gen = self._make_gen_with_sg()
        cloud_seg_no_vnet = {
            "typename": "CloudNetworkSegment",
            "id": "cloud-seg-2",
            "name": "cloud-seg-no-vnet",
            "virtual_network": {},
        }
        src_comp = self._comp(self._onprem_seg(), "fe", "frontend")
        dst_comp = self._comp(cloud_seg_no_vnet, "api", "backend")
        dep = _dep(protocol="tcp", port_start=443)

        result = asyncio.run(gen._create_cloud_rule("myapp", src_comp, dst_comp, dep, "myapp-fe-to-api"))

        assert result is False
        gen._get_or_create_sg.assert_not_called()
        gen.client.create.assert_not_called()

    def test_existing_rule_returns_true_without_create(self):
        gen = _make_gen()
        gen._get_or_create_sg = AsyncMock(return_value=_mock_sg())
        existing_rule = MagicMock()
        existing_rule.save = AsyncMock()
        gen.client.filters = AsyncMock(return_value=[existing_rule])
        gen.client.create = AsyncMock()

        src_comp = self._comp(self._onprem_seg(), "fe", "frontend")
        dst_comp = self._comp(self._cloud_seg(), "api", "backend")
        dep = _dep(protocol="tcp", port_start=443)

        result = asyncio.run(gen._create_cloud_rule("myapp", src_comp, dst_comp, dep, "myapp-fe-to-api"))

        assert result is True
        gen.client.create.assert_not_called()
        existing_rule.save.assert_called_once()

    # ------------------------------------------------------------------
    # Happy-path rule creation
    # ------------------------------------------------------------------

    def test_rule_created_with_correct_fields(self):
        gen = self._make_gen_with_sg()
        src_comp = self._comp(self._onprem_seg(), "fe", "frontend")
        dst_comp = self._comp(self._cloud_seg(), "api", "backend")
        dep = _dep(protocol="tcp", port_start=443)

        result = asyncio.run(gen._create_cloud_rule("myapp", src_comp, dst_comp, dep, "myapp-fe-to-api"))

        assert result is True
        gen.client.create.assert_called_once()
        call_kwargs = gen.client.create.call_args.kwargs
        assert call_kwargs["kind"] == CloudSecurityGroupRule
        rule_data = call_kwargs["data"]
        assert rule_data["security_group"] == {"id": "sg-id-1"}
        assert rule_data["direction"] == "ingress"
        assert rule_data["protocol"] == "tcp"
        assert rule_data["port_start"] == 443
        assert rule_data["action"] == "allow"
        assert rule_data["log"] is True

    def test_source_cidr_set_for_ingress_rule(self):
        gen = self._make_gen_with_sg()
        src_comp = self._comp(
            {"typename": "ManagedVxlanSegment", "id": "s1", "name": "s", "cidr_block": "10.1.0.0/24"},
            "fe",
            "frontend",
        )
        dst_comp = self._comp(self._cloud_seg(), "api", "backend")
        dep = _dep(protocol="tcp", port_start=443)

        asyncio.run(gen._create_cloud_rule("myapp", src_comp, dst_comp, dep, "myapp-fe-to-api"))

        rule_data = gen.client.create.call_args.kwargs["data"]
        assert rule_data["source_cidr"] == "10.1.0.0/24"

    def test_create_failure_returns_false(self):
        gen = _make_gen()
        gen._get_or_create_sg = AsyncMock(return_value=_mock_sg())
        gen.client.filters = AsyncMock(return_value=[])
        gen.client.create = AsyncMock(side_effect=Exception("create failed"))

        src_comp = self._comp(self._onprem_seg(), "fe", "frontend")
        dst_comp = self._comp(self._cloud_seg(), "api", "backend")
        dep = _dep(protocol="tcp", port_start=443)

        result = asyncio.run(gen._create_cloud_rule("myapp", src_comp, dst_comp, dep, "myapp-fe-to-api"))

        assert result is False
        gen.logger.error.assert_called_once()
