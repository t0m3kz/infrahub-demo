"""The plan of AppApplicationGenerator._reconcile_application_rules, applied on the HFID fake: the SecurityPolicyRules of
the dependencies, the isolation modes, what is refused before the first write and the second run."""

from __future__ import annotations

import asyncio
from typing import Any

import pytest

from generators.model.errors import GeneratorError
from tests.unit.app_application_generator_helpers import _app, _backend, _dep, _frontend
from tests.unit.app_plan_helpers import AppClient, make_app_generator


def run(app: dict[str, Any], client: AppClient | None = None) -> tuple[Any, AppClient]:
    """The application reconciled on ``client`` (a fresh one by default): (generator, client)."""
    client = client or AppClient()
    gen = make_app_generator(client)
    asyncio.run(gen._reconcile_application_rules(app))
    return gen, client


def rules(client: AppClient) -> dict[str, dict[str, Any]]:
    """The SecurityPolicyRules written, by name (the last write of a name)."""
    return {data["name"]: data for data in client.saved("SecurityPolicyRule")}


def single_port_app(dst_typename: str = "ManagedVxlanSegment", src_typename: str = "ManagedVxlanSegment") -> dict:
    """One dependency on one port, between segments of the given kinds."""
    return _app(_frontend(_dep(target=_backend(ports=["tcp/8443"], typename=dst_typename)), typename=src_typename))


class TestSegmentRules:
    """Each port of a dependency is one rule of the source segment's egress policy."""

    def test_a_dependency_writes_one_tracked_rule_per_port_in_a_new_default_deny_policy(self) -> None:
        """Empty dependency ports open every port of the target: tcp/8443 and the udp range, indexes 100 and 110."""
        _, client = run(_app(_frontend(_dep(target=_backend()))))

        (policy,) = client.saved("SecurityPolicy")
        assert policy["name"] == "seg-seg-src-name-egress"
        assert policy["default_action"] == "deny"
        assert client.save_kwargs("SecurityPolicy") == [{"allow_upsert": True, "update_group_context": False}]
        tcp, udp = rules(client)["frontend-to-backend-tcp-8443"], rules(client)["frontend-to-backend-udp-30000-30010"]
        assert (tcp["protocol"], tcp["port_start"], tcp["index"]) == ("tcp", 8443, 100)
        assert "port_end" not in tcp
        assert (udp["protocol"], udp["port_start"], udp["port_end"], udp["index"]) == ("udp", 30000, 30010, 110)
        for rule in (tcp, udp):
            assert rule["source_segment"] == {"id": "seg-src"}
            assert rule["destination_segment"] == {"id": "seg-dst"}
            assert rule["policy"] == {"id": "SecurityPolicy-1"}
            assert rule["disabled"] is False
        assert client.save_kwargs("SecurityPolicyRule") == [{"allow_upsert": True}] * 2

    def test_a_second_run_updates_the_rules_in_place_and_creates_nothing(self) -> None:
        """The rules and the policy exist: same ids, same indexes, no new object, every rule saved again (tracked)."""
        _, client = run(_app(_frontend(_dep(target=_backend()))))
        first_ids = {node.id for node in client.store.values()}
        client.saves.clear()

        run(_app(_frontend(_dep(target=_backend()))), client)

        assert {node.id for node in client.store.values()} == first_ids
        assert client.saved("SecurityPolicy") == []
        again = client.saved("SecurityPolicyRule")
        assert sorted(data["name"] for data in again) == [
            "frontend-to-backend-tcp-8443",
            "frontend-to-backend-udp-30000-30010",
        ]
        assert all("index" not in data and "id" in data for data in again)
        assert client.save_kwargs("SecurityPolicyRule") == [{"allow_upsert": True}] * 2

    def test_a_new_rule_takes_the_lowest_free_index_of_a_policy_that_has_rules(self) -> None:
        """Hand-written rules (below 100) and generated ones keep their index; the new one goes to the first gap."""
        client = AppClient()
        policy = client.seed("SecurityPolicy", name="seg-seg-src-name-egress")
        for index, name in ((5, "hand"), (100, "other"), (120, "later")):
            client.seed("SecurityPolicyRule", name=name, index=index, policy={"id": policy.id})

        run(single_port_app(), client)

        assert rules(client)["frontend-to-backend-tcp-8443"]["index"] == 110
        assert client.saved("SecurityPolicy") == []

    def test_the_policy_the_segment_links_is_used_as_it_is(self) -> None:
        """A segment that has a security_policy keeps it: no policy is looked up or created."""
        client = AppClient()
        policy = client.seed("SecurityPolicy", name="hand-written")
        app = single_port_app()
        app["children"][0]["network_segment"]["security_policy"] = {"id": policy.id}

        run(app, client)

        assert rules(client)["frontend-to-backend-tcp-8443"]["policy"] == {"id": policy.id}
        assert client.saved("SecurityPolicy") == []

    def test_an_existing_rule_gets_the_derived_fields_and_loses_a_relationship_it_no_longer_has(self) -> None:
        """The profile and the tag the rule carried are cleared when the planner no longer derives them; its index and
        expiry stay; an expired rule is disabled."""
        client = AppClient()
        policy = client.seed("SecurityPolicy", name="seg-seg-src-name-egress")
        client.seed("SecurityProfile", name="x")
        rule = client.seed(
            "SecurityPolicyRule",
            name="frontend-to-backend-tcp-8443",
            index=130,
            policy={"id": policy.id},
            expires_at="2000-01-01T00:00:00+00:00",
            disabled=False,
            source_security_tag={"id": "tag-old"},
            security_profile={"id": "profile-old"},
        )

        run(single_port_app(), client)

        (saved,) = client.saved("SecurityPolicyRule")
        assert saved["id"] == rule.id
        assert saved["disabled"] is True
        assert "index" not in saved
        assert saved["source_security_tag"] is None
        assert saved["security_profile"] is None

    def test_a_profile_the_database_lacks_leaves_the_rules_profile_as_it_is(self) -> None:
        """A profile named for the flow (cross zone, standard) that does not exist: kept, never cleared."""
        client = AppClient()
        policy = client.seed("SecurityPolicy", name="seg-seg-src-name-egress")
        client.seed(
            "SecurityPolicyRule",
            name="frontend-to-backend-tcp-8443",
            index=100,
            policy={"id": policy.id},
            security_profile={"id": "profile-old"},
        )
        app = single_port_app()
        app["children"][0]["depends_on"][0]["target"]["network_segment"]["security_zone"] = {"name": "DMZ"}

        run(app, client)

        (saved,) = client.saved("SecurityPolicyRule")
        assert "security_profile" not in saved

    def test_a_profile_that_exists_is_attached_to_a_new_rule(self) -> None:
        """internet_exposed over a zone border: the strict profile."""
        client = AppClient()
        profile = client.seed("SecuritySecurityProfile", name="strict")
        app = _app(
            _frontend(_dep(target=_backend(ports=["tcp/8443"]))),
            profile="internet_exposed",
        )
        app["children"][0]["depends_on"][0]["target"]["network_segment"]["security_zone"] = {"name": "DMZ"}

        run(app, client)

        assert rules(client)["frontend-to-backend-tcp-8443"]["security_profile"] == {"id": profile.id}

    def test_a_hand_written_rule_of_the_name_fails_the_run_before_any_write(self) -> None:
        """A rule below index 100 of the same name is never adopted: nothing is written."""
        client = AppClient()
        policy = client.seed("SecurityPolicy", name="seg-seg-src-name-egress")
        client.seed("SecurityPolicyRule", name="frontend-to-backend-tcp-8443", index=10, policy={"id": policy.id})

        with pytest.raises(GeneratorError, match="hand-written"):
            run(single_port_app(), client)

        assert client.saves == []

    def test_a_rule_named_twice_in_a_policy_is_written_once_at_the_first_index(self) -> None:
        """Two dependencies of one name and port: one rule."""
        _, client = run(
            _app(_frontend(_dep(target=_backend(ports=["tcp/8443"])), _dep(target=_backend(ports=["tcp/8443"]))))
        )

        assert [data["name"] for data in client.saved("SecurityPolicyRule")] == ["frontend-to-backend-tcp-8443"]


class TestEndpoints:
    """A cloud or an on-prem segment on either side goes through the same rule."""

    @pytest.mark.parametrize(
        ("src_typename", "dst_typename"),
        [
            ("ManagedVxlanSegment", "CloudNetworkSegment"),
            ("CloudNetworkSegment", "ManagedVxlanSegment"),
            ("CloudNetworkSegment", "CloudNetworkSegment"),
            ("ManagedVxlanSegment", "ManagedVxlanSegment"),
        ],
    )
    def test_every_endpoint_kind_creates_a_security_policy_rule(self, src_typename: str, dst_typename: str) -> None:
        """One rule through the one path, one policy."""
        _, client = run(single_port_app(dst_typename, src_typename))

        (rule,) = client.saved("SecurityPolicyRule")
        assert rule["source_segment"] == {"id": "seg-src"}
        assert rule["destination_segment"] == {"id": "seg-dst"}
        assert len(client.saved("SecurityPolicy")) == 1

    def test_the_egress_policy_of_a_cloud_source_is_qualified_by_its_virtual_network(self) -> None:
        _, client = run(single_port_app(src_typename="CloudNetworkSegment"))

        assert client.saved("SecurityPolicy")[0]["name"] == "seg-seg-src-name-vnet-prod-egress"

    def test_the_cloud_to_other_environment_flow_is_refused(self) -> None:
        """The prod/nonprod guard applies to a cloud endpoint: no rule, no policy."""
        app = single_port_app(dst_typename="CloudNetworkSegment")
        app["children"][0]["depends_on"][0]["target"]["network_segment"]["environment"] = "n"

        _, client = run(app)

        assert client.saves == []

    @pytest.mark.parametrize(("dst_env", "rules_written"), [("s", 0), (None, 0), ("p", 1)])
    def test_the_environment_of_the_destination_segment_decides(self, dst_env: str | None, rules_written: int) -> None:
        """Prod to non-prod and an unknown environment fail closed; prod to prod is ruled."""
        backend = _backend(ports=["tcp/8443"])
        if dst_env is None:
            backend["network_segment"].pop("environment")
        else:
            backend["network_segment"]["environment"] = dst_env

        _, client = run(_app(_frontend(_dep(target=backend))))

        assert len(client.saved("SecurityPolicyRule")) == rules_written


class TestPorts:
    """A rule holds one protocol and port range; what does not parse never becomes an any-port rule."""

    def test_dependency_ports_narrow_the_target_ports(self) -> None:
        _, client = run(_app(_frontend(_dep(ports=["udp/30000-30010"], target=_backend()))))

        assert list(rules(client)) == ["frontend-to-backend-udp-30000-30010"]

    def test_a_port_listed_twice_is_one_rule(self) -> None:
        _, client = run(_app(_frontend(_dep(ports=["tcp/8443", " TCP/8443 "], target=_backend()))))

        assert list(rules(client)) == ["frontend-to-backend-tcp-8443"]

    @pytest.mark.parametrize(
        "bad_port", ["icmp/1", "tcp/0", "tcp/65536", "tcp/9000-8000", "tcp/443-443", "garbage", "tcp/"]
    )
    def test_a_malformed_dependency_port_skips_the_dependency_with_a_warning(self, bad_port: str) -> None:
        gen, client = run(_app(_frontend(_dep(ports=["tcp/8443", bad_port], target=_backend()))))

        assert client.saves == []
        warnings = [call.args for call in gen.logger.warning.call_args_list]
        assert any("frontend-to-backend" in args and "skipping rule creation" in args[0] for args in warnings)

    def test_a_malformed_target_port_skips_the_dependency_with_a_warning(self) -> None:
        gen, client = run(_app(_frontend(_dep(target=_backend(ports=["tcp/8443", "tcp/http"])))))

        assert client.saves == []
        gen.logger.warning.assert_called()

    def test_a_malformed_dependency_does_not_block_the_next_one(self) -> None:
        bad = _dep("bad", ports=["tcp/99999"], target=_backend())
        good = _dep("good", ports=["tcp/8443"], target=_backend())

        _, client = run(_app(_frontend(bad, good)))

        assert list(rules(client)) == ["good-tcp-8443"]

    def test_no_ports_on_the_dependency_or_its_target_is_skipped_with_a_warning(self) -> None:
        gen, client = run(_app(_frontend(_dep(target=_backend(ports=[])))))

        assert client.saves == []
        assert any("has no ports" in call.args[0] for call in gen.logger.warning.call_args_list)

    def test_a_dependency_without_a_target_is_not_an_edge(self) -> None:
        _, client = run(_app(_frontend(_dep("fe-to-nowhere", ports=["tcp/443"]))))

        assert client.saves == []

    def test_a_denied_dependency_is_skipped_before_any_port_is_resolved(self) -> None:
        gen, client = run(_app(_frontend({**_dep(target=_backend()), "access_status": "denied"})))

        assert client.saves == []
        assert any("is not authorized" in call.args[0] for call in gen.logger.warning.call_args_list)


class TestIsolationMode:
    """A segment's isolation_mode is derived once from its application's security profile."""

    def test_fintech_strict_microsegments_each_segment_once(self) -> None:
        """Two components on one segment: one write; an author-set non-default and a cloud segment are left alone."""
        app = {
            "name": "fraud",
            "security_profile": "fintech_strict",
            "children": [
                {
                    "id": "c-1",
                    "fqdn": "c1.example",
                    "network_segment": {"id": "seg-1", "name": "s1", "typename": "ManagedVxlanSegment"},
                },
                {
                    "id": "c-2",
                    "fqdn": "c2.example",
                    "network_segment": {"id": "seg-1", "name": "s1", "typename": "ManagedVxlanSegment"},
                },
                {"id": "c-3", "network_segment": {"id": "seg-2", "typename": "CloudNetworkSegment"}},
                {
                    "id": "c-4",
                    "network_segment": {
                        "id": "seg-3",
                        "typename": "ManagedVxlanSegment",
                        "isolation_mode": "custom",
                    },
                },
            ],
        }

        _, client = run(app)

        assert client.saved("ManagedVxlanSegment") == [{"id": "seg-1", "isolation_mode": "microsegmented"}]
        assert client.save_kwargs("ManagedVxlanSegment") == [{"allow_upsert": True, "update_group_context": False}]

    def test_a_component_without_a_segment_writes_nothing(self) -> None:
        _, client = run({"name": "fraud", "security_profile": "fintech_strict", "children": [{"id": "c-1"}]})

        assert client.saves == []

    def test_an_application_without_edges_still_derives_its_modes(self) -> None:
        app = {
            "name": "fraud",
            "security_profile": "fintech_strict",
            "children": [
                {
                    "id": "c-1",
                    "fqdn": "c1.example",
                    "network_segment": {"id": "seg-1", "name": "s1", "typename": "ManagedVxlanSegment"},
                }
            ],
        }

        gen, client = run(app)

        assert client.saved("ManagedVxlanSegment") == [{"id": "seg-1", "isolation_mode": "microsegmented"}]
        assert any("no depends_on edges" in call.args[0] for call in gen.logger.info.call_args_list)
