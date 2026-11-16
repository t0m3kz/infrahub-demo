"""The application rules around a load balanced component, applied on the HFID fake: R_client replaces the direct rule,
and what a run saves into its group (tracked) is what the next run keeps."""

from __future__ import annotations

from typing import Any
from unittest.mock import AsyncMock, patch

import pytest

from generators.model.errors import GeneratorError
from tests.unit.app_plan_helpers import AppClient
from tests.unit.lb_vip_helpers import (
    _client_comp,
    _cloud_frontend,
    _context,
    _dep,
    _instance,
    _web,
    application,
    run_application,
)


def rules(client: AppClient) -> dict[str, dict[str, Any]]:
    """The SecurityPolicyRules written, by name."""
    return {data["name"]: data for data in client.saved("SecurityPolicyRule")}


def tracked(client: AppClient) -> set[tuple[str, str]]:
    """(kind, name) of what the run saved into its group: a save that did not ask to stay out of it."""
    return {
        (kind, str(data.get("name") or f"{data.get('hostname')}:{data.get('port')}"))
        for kind, _id, data, kwargs in client.saves
        if kwargs.get("update_group_context", True)
    }


class TestClientRule:
    def test_r_client_replaces_the_direct_rule(self) -> None:
        _, client = run_application(application(_client_comp(_dep(_web()))))

        data = rules(client)["portal-to-web-tcp-443-vip-dc10-lb-prod"]
        assert [name for name in rules(client) if name.startswith("portal-to-web")] == [
            "portal-to-web-tcp-443-vip-dc10-lb-prod"
        ]
        vip_address = next(
            node
            for node in client.address_pool_allocations.values()
            if node.data["identifier"] == "comp-web-tcp-443-vip"
        )
        assert data["destination_ip_addresses"] == [{"id": vip_address.id}]
        assert "destination_segment" not in data
        # The VIP has no segment: the target's tenant zone (the context's VIP range is in it) qualifies it.
        assert data["destination_zone"] == {"hfid": ["PROD"]}
        assert data["source_segment"] == {"id": "seg-portal"}
        assert (data["protocol"], data["port_start"]) == ("tcp", 443)

    def test_one_r_client_per_vip(self) -> None:
        """VIPs on two contexts: two rules, each naming one VIP (the ACL renders one address selector)."""
        other = _context("ctx-2", "dc12-lb-PROD", leg="leg-2", vip_prefix="vip-pfx-2")
        web = _web(instances=[_instance("vm-1", _context()), _instance("vm-2", other)])

        _, client = run_application(application(_client_comp(_dep(web))))

        clients = {name: data for name, data in rules(client).items() if name.startswith("portal-to-web")}
        assert sorted(clients) == ["portal-to-web-tcp-443-vip-dc10-lb-prod", "portal-to-web-tcp-443-vip-dc12-lb-prod"]
        assert all(len(data["destination_ip_addresses"]) == 1 for data in clients.values())
        assert len({data["destination_ip_addresses"][0]["id"] for data in clients.values()}) == 2

    def test_a_target_that_is_not_load_balanced_keeps_the_direct_rule(self) -> None:
        _, client = run_application(application(_client_comp(_dep(_web(load_balanced=False)))))

        assert list(rules(client)) == ["portal-to-web-tcp-443"]
        assert rules(client)["portal-to-web-tcp-443"]["destination_segment"] == {"id": "seg-web"}

    def test_a_port_without_a_vip_fails_the_run_before_any_write(self) -> None:
        """Strict: no VIP on a port means no rule at all, never the direct one. The target's context has no VIP range."""
        web = _web(instances=[_instance("vm-1", _context(vip_prefix=None))])
        client = AppClient()

        with (
            patch("generators.applications.lb_vip.asyncio.sleep", AsyncMock()),
            pytest.raises(GeneratorError, match="load-balanced target web.c001.example has no VIP on tcp/443"),
        ):
            run_application(application(_client_comp(_dep(web))), client)

        assert client.saves == []

    def test_producer_errors_fail_the_run_without_edges_too(self) -> None:
        client = AppClient()

        with pytest.raises(GeneratorError, match="port range"):
            run_application(application(_web(ports=["tcp/443", "udp/1-2"])), client)

        assert client.saves == []

    def test_no_load_balancing_error_does_not_fail(self) -> None:
        _, client = run_application(application(_web()))

        assert len(client.saved("LoadbalancerVIP")) == 1


class TestLoadBalancedFlip:
    @staticmethod
    def _run(web_load_balanced: bool) -> set[tuple[str, str]]:
        """One add_app_application run of an application whose portal calls its own web tier."""
        web = _web(load_balanced=web_load_balanced)
        portal = _client_comp(_dep(dict(web)))
        _, client = run_application(application(portal, web))
        return tracked(client)

    def test_turning_load_balanced_off_leaves_vip_and_both_rules_to_the_cleanup(self) -> None:
        """delete_unused_nodes removes what the earlier run tracked and this one did not save."""
        on = self._run(web_load_balanced=True)
        off = self._run(web_load_balanced=False)

        assert on - off == {
            ("LoadbalancerVIP", "web.c001.example:443"),
            ("SecurityPolicyRule", "web.c001.example-tcp-443"),
            ("SecurityPolicyRule", "portal-to-web-tcp-443-vip-dc10-lb-prod"),
        }
        assert off - on == {("SecurityPolicyRule", "portal-to-web-tcp-443")}

    def test_shared_objects_are_never_tracked(self) -> None:
        web = _web()
        _, client = run_application(application(_client_comp(_dep(dict(web))), web))

        kinds = {kind for kind, _ in tracked(client)}
        assert kinds == {"LoadbalancerVIP", "SecurityPolicyRule"}


class TestCloudTracking:
    def test_turning_load_balanced_off_leaves_the_load_balancer_and_its_vips_to_the_cleanup(self) -> None:
        _, on = run_application(application(_cloud_frontend(load_balanced=True)))
        _, off = run_application(application(_cloud_frontend(load_balanced=False)))

        assert {kind for kind, _ in tracked(on)} == {"CloudLoadBalancer", "LoadbalancerVIP"}
        assert tracked(off) == set()


class TestCloudClientRule:
    def test_a_dependency_into_a_cloud_target_keeps_the_direct_rule_and_no_vip_error(self) -> None:
        """No R_client for a cloud target: the 'has no VIP on port' error is for an on-prem one only."""
        _, client = run_application(application(_client_comp(_dep(_cloud_frontend()))))

        data = rules(client)["portal-to-web-tcp-443"]
        assert data["destination_segment"] == {"id": "sub-a"}
        assert "destination_ip_addresses" not in data

    def test_the_application_security_profile_reaches_the_load_balancer(self) -> None:
        _, client = run_application(application(_cloud_frontend(), profile="internet_exposed"))

        assert client.saved("CloudLoadBalancer")[0]["scheme"] == "internet_facing"
