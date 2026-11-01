"""CheckProviderConnections: rules PC1 to PC13, a passing case and a failing case each.

Payload builders: tests/unit/provider_connections_check_helpers.py.
"""

from __future__ import annotations

from typing import Any

import pytest

from tests.unit.provider_connection_helpers import dual_link_profile  # noqa: F401
from tests.unit.provider_connections_check_helpers import (
    AM_NE,
    C001,
    C002,
    DC10,
    EUC1,
    FR2,
    XC,
    account,
    carrier,
    connection,
    edges,
    kit_end,
    namespace,
    node,
    place,
    port,
    prefix,
    run,
    session,
)

pytestmark = pytest.mark.usefixtures("dual_link_profile")


BASE = connection()
SESSION = session("PC-AWS-FR2-L1")


def test_reference_connection_passes() -> None:
    """A dedicated AWS connection with its handoff session, carrier and VLAN has no finding."""
    errors, _ = run([BASE], [SESSION], [carrier("XC-FR2-EUC1", [FR2, EUC1], ["PC-AWS-FR2"])])
    assert errors == []


def test_decommissioned_connection_is_exempt() -> None:
    """Nothing is built for a decommissioned connection: it is not judged."""
    assert run([connection(status="decommissioned", vlan_id=None)], [])[0] == []


class TestPc1SessionsBelongToAConnection:
    """A handoff or vpn that lands in a provider-hosted place has a connection."""

    def test_session_without_a_connection_fails(self) -> None:
        errors, _ = run([], [session("HO-1", connection=None)])
        assert len(errors) == 1 and errors[0].startswith("PC1: handoff circuit 'HO-1' lands in eu-central-1")

    def test_session_between_our_own_places_passes(self) -> None:
        """A handoff between two zones of ours (the SD-WAN gateway LAN) is no provider connection."""
        ends = [port("a", "e1.10", FR2), port("b", "e2.10", FR2)]
        assert run([], [session("HO-2", connection=None, ends=ends, locations=[FR2])])[0] == []

    def test_virtual_colocation_is_provider_hosted(self) -> None:
        ends = [port("eg-fr201", "e1.10", FR2), port("ne", "g1.10", AM_NE)]
        errors, _ = run([], [session("HO-3", connection=None, ends=ends, locations=[FR2, AM_NE])])
        assert len(errors) == 1 and errors[0].startswith("PC1")

    def test_decommissioned_session_is_exempt(self) -> None:
        assert run([], [session("HO-1", connection=None, status="decommissioned")])[0] == []


class TestPc2CarriersBelongToAConnection:
    def test_provider_hosted_carrier_without_a_connection_fails(self) -> None:
        errors, _ = run([], [], [carrier("XC-FR2-EUC1", [FR2, EUC1], [])])
        assert len(errors) == 1 and errors[0].startswith("PC2: carrier circuit 'XC-FR2-EUC1' ends in eu-central-1")

    def test_carrier_in_several_connections_passes(self) -> None:
        assert run([], [], [carrier("XC-FR2-EUC1", [FR2, EUC1], ["PC-1", "PC-2"])])[0] == []

    def test_zone_to_zone_carrier_is_exempt(self) -> None:
        other = place("FR6", "TopologyColocationZone")
        assert run([], [], [carrier("XC-FR2-FR6", [FR2, other], [])])[0] == []

    def test_dc_to_zone_carrier_is_exempt(self) -> None:
        assert run([], [], [carrier("DF-DC10-FR2", [DC10, FR2], [])])[0] == []


class TestPc3Flavour:
    def test_unlisted_pair_for_a_profile_fails(self) -> None:
        errors, _ = run([connection(profile="zscaler")], [SESSION])
        assert any(e.startswith("PC3") and "no dedicated flavour" in e for e in errors)

    def test_dedicated_without_vlan_fails(self) -> None:
        errors, _ = run([connection(vlan_id=None)], [SESSION])
        assert any("PC3: dedicated connection 'PC-AWS-FR2' has no vlan_id" in e for e in errors)

    def test_session_count_must_match_the_flavour(self) -> None:
        """A two-link flavour; one session is a finding."""
        conn = connection(profile="dual", link_prefixes=[prefix("10.1.0.0/30"), prefix("10.1.0.4/30")])
        errors, _ = run([conn], [session("S1", link_prefix="10.1.0.0/30")])
        assert any(
            "PC3: connection 'PC-AWS-FR2' has 1 session circuit(s) but its flavour (dual, dedicated) carries 2" in e
            for e in errors
        )

    def test_two_sessions_for_a_two_link_flavour_pass_pc3(self) -> None:
        conn = connection(profile="dual", link_prefixes=[prefix("10.1.0.0/30"), prefix("10.1.0.4/30")])
        errors, _ = run([conn], [session("S1", link_prefix="10.1.0.0/30"), session("S2", link_prefix="10.1.0.4/30")])
        assert [e for e in errors if e.startswith("PC3")] == []

    def test_session_type_must_follow_the_path_type(self) -> None:
        errors, _ = run([BASE], [session("S1", circuit_type="vpn")])
        assert any("PC3: session 'S1' is a vpn circuit but connection 'PC-AWS-FR2' is dedicated" in e for e in errors)

    def test_network_and_gateway_together_fail(self) -> None:
        errors, _ = run([connection(attached_gateway=True, attached_network=True)], [SESSION])
        assert any(
            "PC3: connection 'PC-AWS-FR2' attaches to both a virtual network and a transit gateway" in e for e in errors
        )

    @pytest.mark.parametrize(("gateway", "network"), [(True, False), (False, True), (False, False)])
    def test_at_most_one_attachment_passes(self, gateway: bool, network: bool) -> None:
        errors, _ = run([connection(attached_gateway=gateway, attached_network=network)], [SESSION])
        assert [e for e in errors if e.startswith("PC3")] == []

    def test_default_flavour_without_a_profile_passes(self) -> None:
        errors, _ = run([connection(profile=None)], [SESSION])
        assert [e for e in errors if e.startswith("PC3")] == []


class TestPc4Vlan:
    def test_session_on_another_vlan_fails(self) -> None:
        ends = [
            port("eg-fr201", "Ethernet1/13.3102", FR2, (XC,)),
            port("EXT-AWS-DX-FR2", "et-0/0/0.3102", EUC1, asns=(64512,)),
        ]
        errors, _ = run([BASE], [session("S1", ends=ends)])
        assert any(
            "PC4: session 'S1' end 'eg-fr201:Ethernet1/13.3102' is on VLAN 3102 but connection 'PC-AWS-FR2' is on VLAN 3101"
            in e
            for e in errors
        )

    def test_end_that_is_no_sub_interface_fails(self) -> None:
        ends = [
            port("eg-fr201", "Ethernet1/13", FR2, (XC,)),
            port("EXT-AWS-DX-FR2", "et-0/0/0.3101", EUC1, asns=(64512,)),
        ]
        errors, _ = run([BASE], [session("S1", ends=ends)])
        assert any("PC4: session 'S1' end 'eg-fr201:Ethernet1/13' is not named <port>.<vlan>" in e for e in errors)

    def test_vpn_ends_are_not_vlan_checked(self) -> None:
        conn = connection(
            path_type="internet_vpn",
            vlan_id=None,
            profile="aws",
            link_prefixes=[prefix("169.254.1.0/30"), prefix("169.254.2.0/30")],
            carriers=[],
        )
        ends = [port("bl", "Tunnel1", DC10, sub=False), port("EXT-AWS-VPN", "Tunnel1", EUC1, asns=(64512,), sub=False)]
        sessions = [session(f"S{i}", circuit_type="vpn", ends=ends, link_prefix=f"169.254.{i}.0/30") for i in (1, 2)]
        errors, _ = run([conn], sessions)
        assert [e for e in errors if e.startswith("PC4")] == []


class TestPc5Owner:
    def test_account_with_an_owner_fails(self) -> None:
        errors, _ = run([connection(account=account(owner=C001), owner=C001)], [SESSION])
        assert any("PC5: connection 'PC-AWS-FR2' has an account and an owner of its own" in e for e in errors)

    def test_account_alone_passes(self) -> None:
        assert [e for e in run([connection(account=account(owner=C001))], [SESSION])[0] if e.startswith("PC5")] == []

    def test_registered_prefixes_need_an_owner(self) -> None:
        conn = connection(registered=[prefix("100.64.0.0/24")])
        errors, _ = run([conn], [SESSION])
        assert any("PC5: connection 'PC-AWS-FR2' registers prefixes but has no customer owner" in e for e in errors)

    def test_registered_prefixes_with_an_owner_pass(self) -> None:
        conn = connection(owner=C002, registered=[prefix("100.64.0.0/24")])
        assert [e for e in run([conn], [SESSION])[0] if e.startswith("PC5")] == []

    def test_registered_prefixes_with_the_accounts_owner_pass(self) -> None:
        conn = connection(account=account(owner=C001), registered=[prefix("100.64.0.0/24")])
        assert [e for e in run([conn], [SESSION])[0] if e.startswith("PC5")] == []

    def test_prefix_outside_the_namespace_fails(self) -> None:
        conn = connection(owner=C002, registered=[prefix("100.64.0.0/24", ns="NON-PROD")])
        errors, _ = run([conn], [SESSION])
        assert any(
            "registers 100.64.0.0/24 of namespace 'NON-PROD' but its ip_namespace is 'PROD'" in e for e in errors
        )

    def test_non_customer_owner_does_not_count(self) -> None:
        provider = {"id": "id-p", "__typename": "OrganizationProvider", "org_id": {"value": "P"}, "display_label": "P"}
        errors, _ = run([connection(owner=provider, registered=[prefix("100.64.0.0/24")])], [SESSION])
        assert any("no customer owner" in e for e in errors)


class TestPc6Stubs:
    def test_two_processes_on_a_stub_fail(self) -> None:
        ends = [
            port("eg-fr201", "Ethernet1/13.3101", FR2, (XC,)),
            port("EXT-AWS-DX-FR2", "et-0/0/0.3101", EUC1, asns=(64512, 64513)),
        ]
        errors, _ = run([BASE], [session("S1", ends=ends)])
        assert any(
            "PC6: provider-side device 'EXT-AWS-DX-FR2' of connection 'PC-AWS-FR2' has 2 BGP process(es)" in e
            for e in errors
        )

    def test_no_process_fails(self) -> None:
        ends = [port("eg-fr201", "Ethernet1/13.3101", FR2, (XC,)), port("EXT-AWS-DX-FR2", "et-0/0/0.3101", EUC1)]
        errors, _ = run([BASE], [session("S1", ends=ends)])
        assert any("has 0 BGP process(es)" in e for e in errors)

    def test_provider_as_must_match_the_stub(self) -> None:
        errors, _ = run([connection(provider_as=64999)], [SESSION])
        assert any("runs BGP in AS 64512 but connection 'PC-AWS-FR2' states provider_as 64999" in e for e in errors)

    def test_provider_as_matching_passes(self) -> None:
        assert [e for e in run([connection(provider_as=64512)], [SESSION])[0] if e.startswith("PC6")] == []

    def test_two_nics_of_one_service_edge_appliance_are_one_stub_with_one_process(self) -> None:
        """A region's service edge appliance serves several legs on distinct NICs: still ONE provider-side device."""
        conn = connection(profile="dual", link_prefixes=[prefix("10.1.0.0/30"), prefix("10.1.0.4/30")])
        sessions = [
            session(
                f"S{n}",
                ends=[
                    port("eg-fr201", f"Ethernet1/1{n}.3101", FR2, (XC,)),
                    port("AWS-EUC1-SE", f"vnic-{n}.3101", EUC1, asns=(64512,)),
                ],
                link_prefix=f"10.1.0.{4 * (n - 1)}/30",
            )
            for n in (1, 2)
        ]
        errors, _ = run([conn], sessions)
        assert [e for e in errors if e.startswith("PC6")] == []

    def test_stubs_in_different_ases_fail(self) -> None:
        conn = connection(profile="dual", link_prefixes=[prefix("10.1.0.0/30"), prefix("10.1.0.4/30")])
        s1 = session("S1", link_prefix="10.1.0.0/30")
        ends = [
            port("eg-fr201", "Ethernet1/13.3101", FR2, (XC,)),
            port("EXT-AZ-2", "et-0/0/1.3101", EUC1, asns=(12076,)),
        ]
        s2 = session("S2", ends=ends, link_prefix="10.1.0.4/30")
        errors, _ = run([conn], [s1, s2])
        assert any("run BGP in different ASes" in e for e in errors)


class TestPc7LinkPrefixes:
    def _dual(self, prefixes: list[dict[str, Any]]) -> dict[str, Any]:
        return connection(profile="dual", link_prefixes=prefixes)

    def test_provider_assigned_pair_passes(self) -> None:
        conn = self._dual([prefix("10.1.0.0/30"), prefix("10.1.0.4/30")])
        errors, _ = run([conn], [session("S1", link_prefix="10.1.0.0/30"), session("S2", link_prefix="10.1.0.4/30")])
        assert [e for e in errors if e.startswith("PC7")] == []

    def test_count_mismatch_fails(self) -> None:
        errors, _ = run(
            [self._dual([prefix("10.1.0.0/30")])],
            [session("S1", link_prefix="10.1.0.0/30"), session("S2", link_prefix="10.1.0.0/30")],
        )
        assert any("lists 1 link prefix(es) but its flavour is provider-assigned with 2 link(s)" in e for e in errors)

    def test_wrong_length_fails(self) -> None:
        conn = self._dual([prefix("10.1.0.0/29"), prefix("10.1.0.8/30")])
        errors, _ = run([conn], [session("S1", link_prefix="10.1.0.0/29"), session("S2", link_prefix="10.1.0.8/30")])
        assert any("link prefix 10.1.0.0/29 of connection 'PC-AWS-FR2' is not a /30" in e for e in errors)

    def test_wrong_namespace_fails(self) -> None:
        conn = self._dual([prefix("10.1.0.0/30", ns="NON-PROD"), prefix("10.1.0.4/30")])
        errors, _ = run([conn], [session("S1", link_prefix="10.1.0.0/30"), session("S2", link_prefix="10.1.0.4/30")])
        assert any("is in namespace 'NON-PROD' but connection 'PC-AWS-FR2' is in 'PROD'" in e for e in errors)

    def test_session_without_a_link_prefix_fails(self) -> None:
        conn = self._dual([prefix("10.1.0.0/30"), prefix("10.1.0.4/30")])
        errors, _ = run([conn], [session("S1", link_prefix="10.1.0.0/30"), session("S2")])
        assert any("PC7: session 'S2' has no link_prefix" in e for e in errors)

    def test_session_with_an_unlisted_link_prefix_fails(self) -> None:
        conn = self._dual([prefix("10.1.0.0/30"), prefix("10.1.0.4/30")])
        errors, _ = run([conn], [session("S1", link_prefix="10.1.0.0/30"), session("S2", link_prefix="10.9.0.0/30")])
        assert any("link_prefix 10.9.0.0/30 is not among the link_prefixes" in e for e in errors)

    def test_our_pool_flavour_has_none(self) -> None:
        errors, _ = run([connection(link_prefixes=[prefix("10.1.0.0/31")])], [SESSION])
        assert any("PC7" in e and "allocates the link addresses from our pool" in e for e in errors)


class TestPc8VpnUnrendered:
    def _vpn(self) -> tuple[dict[str, Any], list[dict[str, Any]]]:
        conn = connection(
            near=DC10,
            path_type="internet_vpn",
            vlan_id=None,
            link_prefixes=[prefix("169.254.1.0/30"), prefix("169.254.2.0/30")],
            carriers=[],
        )
        ends = [port("bl", "Tunnel1", DC10, sub=False), port("EXT-AWS-VPN", "Tunnel1", EUC1, asns=(64512,), sub=False)]
        return conn, [session(f"S{i}", circuit_type="vpn", ends=ends, link_prefix=f"169.254.{i}.0/30") for i in (1, 2)]

    def test_vpn_session_warns_unrendered(self) -> None:
        conn, sessions = self._vpn()
        errors, infos = run([conn], sessions)
        assert errors == []
        warnings = [i for i in infos if i.startswith("WARNING: PC8")]
        assert len(warnings) == 2 and "is unrendered" in warnings[0]

    def test_dedicated_connection_does_not_warn(self) -> None:
        _, infos = run([BASE], [SESSION])
        assert not [i for i in infos if "PC8" in i]


class TestPc9Environment:
    """The namespace class is the account's environment class."""

    def test_prod_namespace_on_a_non_prod_account_fails(self) -> None:
        errors, _ = run([connection(account=account(env="d"))], [SESSION])
        assert any(
            "PC9: connection 'PC-AWS-FR2' is in namespace 'PROD' (prod) but its account 'acct-1' is environment 'd' (non_prod)"
            in e
            for e in errors
        )

    def test_matching_environment_passes(self) -> None:
        conn = connection(account=account(env="s"), ns=namespace("NON-PROD", "non_prod"))
        assert [e for e in run([conn], [SESSION])[0] if "namespace" in e] == []


class TestPc10SessionTypes:
    """A dci or a tunnel is routed on no provider connection."""

    def test_dci_in_a_connection_fails(self) -> None:
        errors, _ = run([BASE], [session("DCI-1", circuit_type="dci")])
        assert any("PC10: dci circuit 'DCI-1' belongs to provider connection 'PC-AWS-FR2'" in e for e in errors)

    def test_tunnel_in_a_connection_fails(self) -> None:
        errors, _ = run([BASE], [session("T-1", circuit_type="tunnel")])
        assert any("PC10: tunnel circuit 'T-1'" in e for e in errors)


class TestPc11CarriersOutput:
    """carrier_circuits is the generator's output: sanity, at WARNING level (it can lag the generator)."""

    @staticmethod
    def _warnings(infos: list[str], rule: str) -> list[str]:
        return [i for i in infos if i.startswith(f"WARNING: {rule}")]

    def test_listed_carrier_in_the_near_facility_passes(self) -> None:
        _, infos = run([BASE], [SESSION])
        assert self._warnings(infos, "PC11") == []

    def test_dedicated_with_sessions_and_no_carrier_warns(self) -> None:
        errors, infos = run([connection(carriers=[])], [SESSION])
        assert [e for e in errors if "PC11" in e] == []
        assert len(self._warnings(infos, "PC11")) == 1 and "lists no carrier circuit" in infos[-1]

    def test_dedicated_without_sessions_needs_no_carrier(self) -> None:
        _, infos = run([connection(carriers=[])], [])
        assert self._warnings(infos, "PC11") == []

    def test_carrier_without_an_end_in_the_near_facility_warns(self) -> None:
        conn = connection()
        conn["carrier_circuits"]["edges"][0]["node"]["locations"] = edges([{"id": EUC1["id"]}])
        conn["carrier_circuits"]["edges"][0]["node"]["capabilities"] = edges([kit_end("x", "e1", EUC1)])
        _, infos = run([conn], [SESSION])
        assert any("PC11: carrier circuit 'XC-FR2-EUC1' of connection 'PC-AWS-FR2' has no end" in i for i in infos)

    def test_carrier_with_a_near_end_passes_without_locations(self) -> None:
        conn = connection()
        node_ = conn["carrier_circuits"]["edges"][0]["node"]
        node_["locations"] = edges([])
        node_["capabilities"] = edges([kit_end("eg-fr201", "Ethernet1/13", FR2)])
        _, infos = run([conn], [SESSION])
        assert self._warnings(infos, "PC11") == []

    def test_decommissioned_carrier_is_not_listed(self) -> None:
        conn = connection()
        conn["carrier_circuits"]["edges"][0]["node"]["status"] = {"value": "decommissioned"}
        _, infos = run([conn], [SESSION])
        assert "lists no carrier circuit" in infos[-1]

    def test_internet_vpn_needs_no_carrier(self) -> None:
        _, infos = run([connection(path_type="internet_vpn", vlan_id=None, carriers=[])], [])
        assert self._warnings(infos, "PC11") == []


class TestPc12NearPort:
    """An explicit near_port is an end of one of the listed carriers."""

    def _with_end(self, port_id: str) -> dict[str, Any]:
        conn = connection(near_port="eg-fr201:Ethernet1/13")
        conn["carrier_circuits"]["edges"][0]["node"]["capabilities"] = edges(
            [{"id": port_id, "device": node({"id": "id-d", "deployment": node({"id": FR2["id"]})})}]
        )
        return conn

    def test_near_port_that_is_a_carrier_end_passes(self) -> None:
        errors, _ = run([self._with_end("eg-fr201:Ethernet1/13")], [SESSION])
        assert [e for e in errors if "PC12" in e] == []

    def test_near_port_that_is_no_carrier_end_fails(self) -> None:
        errors, _ = run([self._with_end("eg-fr201:Ethernet1/2")], [SESSION])
        assert any("PC12: near_port of connection 'PC-AWS-FR2' is not an end of any" in e for e in errors)

    def test_no_near_port_is_not_judged(self) -> None:
        errors, _ = run([BASE], [SESSION])
        assert [e for e in errors if "PC12" in e] == []

    def test_near_port_with_no_carrier_fails(self) -> None:
        errors, _ = run([connection(near_port="eg-fr201:Ethernet1/13", carriers=[])], [SESSION])
        assert any("PC12" in e for e in errors)

    @staticmethod
    def _internet_carrier(end_port: str, status: str = "active", circuit_type: str = "internet") -> dict[str, Any]:
        """A root carrier circuit of FR2 with one end on ``end_port``."""
        root = carrier("INET-FR2", [FR2], [], ends=[kit_end(end_port.split(":")[0], end_port.split(":")[1], FR2)])
        root["status"] = {"value": status}
        root["circuit_type"] = {"value": circuit_type}
        return root

    @staticmethod
    def _vpn(near_port: str) -> dict[str, Any]:
        return connection(path_type="internet_vpn", vlan_id=None, carriers=[], near_port=near_port)

    def test_vpn_near_port_on_an_internet_carrier_end_passes(self) -> None:
        carriers = [self._internet_carrier("eg-fr201:Ethernet1/14")]
        errors, _ = run([self._vpn("eg-fr201:Ethernet1/14")], [], carriers)
        assert [e for e in errors if "PC12" in e] == []

    def test_vpn_near_port_that_is_no_internet_carrier_end_fails(self) -> None:
        carriers = [self._internet_carrier("eg-fr201:Ethernet1/14")]
        errors, _ = run([self._vpn("eg-fr201:Ethernet1/13")], [], carriers)
        assert any(
            "PC12: near_port of VPN connection 'PC-AWS-FR2' is not an end of an internet carrier" in e for e in errors
        )

    @pytest.mark.parametrize(("status", "circuit_type"), [("decommissioned", "internet"), ("active", "cross_connect")])
    def test_vpn_near_port_on_a_dead_or_non_internet_carrier_fails(self, status: str, circuit_type: str) -> None:
        carriers = [self._internet_carrier("eg-fr201:Ethernet1/14", status=status, circuit_type=circuit_type)]
        errors, _ = run([self._vpn("eg-fr201:Ethernet1/14")], [], carriers)
        assert any("PC12: near_port of VPN connection" in e for e in errors)

    def test_dedicated_near_port_is_still_judged_against_the_listed_carriers(self) -> None:
        """An internet carrier ending on the port does not make it the near port of a dedicated connection."""
        conn = connection(near_port="eg-fr201:Ethernet1/14")
        errors, _ = run([conn], [SESSION], [self._internet_carrier("eg-fr201:Ethernet1/14")])
        assert any("PC12: near_port of connection 'PC-AWS-FR2' is not an end of any" in e for e in errors)


class TestPc13Redundancy:
    """Two legs of one redundancy group on one near device are a WARNING."""

    @staticmethod
    def _run(devices: tuple[str, str], group: str | None = "RG-1", status: str = "active") -> list[str]:
        conn = connection(carriers=["XC-A", "XC-B"])
        carriers = [
            carrier(
                "XC-A",
                [FR2, EUC1],
                ["PC-AWS-FR2"],
                group,
                [kit_end(devices[0], "e1", FR2), kit_end("far-1", "x", EUC1)],
            ),
            carrier(
                "XC-B",
                [FR2, EUC1],
                ["PC-AWS-FR2"],
                group,
                [kit_end(devices[1], "e1", FR2), kit_end("far-1", "y", EUC1)],
                status,
            ),
        ]
        for edge in conn["carrier_circuits"]["edges"]:
            edge["node"]["capabilities"] = edges([kit_end("eg-fr201", "e", FR2)])
        errors, infos = run([conn], [SESSION], carriers)
        assert errors == [] or all("PC13" not in e for e in errors)
        return [i for i in infos if i.startswith("WARNING: PC13")]

    def test_two_legs_on_one_device_warn(self) -> None:
        warnings = self._run(("eg-fr201", "eg-fr201"))
        assert len(warnings) == 1 and "redundancy group 'RG-1' has 2 carrier circuits" in warnings[0]
        assert "end on 1 device(s) (eg-fr201)" in warnings[0]

    def test_two_legs_on_two_devices_pass(self) -> None:
        assert self._run(("eg-fr201", "eg-fr202")) == []

    def test_no_group_is_not_judged(self) -> None:
        assert self._run(("eg-fr201", "eg-fr201"), group=None) == []

    def test_decommissioned_leg_is_ignored(self) -> None:
        assert self._run(("eg-fr201", "eg-fr201"), status="decommissioned") == []

    def test_far_side_devices_do_not_count(self) -> None:
        """Both legs share the far device but have distinct near devices: no finding."""
        assert self._run(("eg-fr201", "eg-fr202")) == []


class TestCloudConsistency:
    def _segment(self, owner: str, footprint_owner: str) -> dict[str, Any]:
        return {
            "id": "id-seg",
            "name": {"value": "c001-web"},
            "owner": {"node": {"id": f"id-{owner}", "org_id": {"value": owner}}},
            "customer_deployment": {
                "node": {
                    "id": "id-fp",
                    "name": {"value": "C001-CLOUD"},
                    "owner": {"node": {"id": f"id-{footprint_owner}", "org_id": {"value": footprint_owner}}},
                }
            },
        }

    def test_segment_with_the_footprint_owner_passes(self) -> None:
        assert run(segments=[self._segment("C001", "C001")])[0] == []

    def test_segment_with_another_owner_fails(self) -> None:
        errors, _ = run(segments=[self._segment("C001", "C002")])
        assert (
            len(errors) == 1
            and "Cloud segment 'c001-web' is owned by 'C001' but its footprint 'C001-CLOUD' by 'C002'" in errors[0]
        )

    @staticmethod
    def _attachment(kind: str, owner_account: str, network_account: str) -> dict[str, Any]:
        """A network attachment (``vpc``) or a gateway-to-gateway one (anything else: no virtual network)."""
        if kind != "vpc":
            return {
                "id": "id-att",
                "name": {"value": "att-1"},
                "owner_account": {"node": {"id": f"id-{owner_account}", "name": {"value": owner_account}}},
                "virtual_network": {"node": None},
            }
        return {
            "id": "id-att",
            "name": {"value": "att-1"},
            "owner_account": {"node": {"id": f"id-{owner_account}", "name": {"value": owner_account}}},
            "virtual_network": {
                "node": {
                    "id": "id-vnet",
                    "name": {"value": "vnet-1"},
                    "account": {"node": {"id": f"id-{network_account}", "name": {"value": network_account}}},
                }
            },
        }

    def test_vpc_attachment_in_the_network_account_passes(self) -> None:
        assert run(attachments=[self._attachment("vpc", "acct-1", "acct-1")])[0] == []

    def test_vpc_attachment_in_another_account_fails(self) -> None:
        errors, _ = run(attachments=[self._attachment("vpc", "acct-1", "acct-2")])
        assert (
            len(errors) == 1
            and "(network target) lives in account 'acct-1' but its virtual network 'vnet-1' is in account 'acct-2'"
            in errors[0]
        )

    def test_a_gateway_target_attachment_is_not_checked(self) -> None:
        assert run(attachments=[self._attachment("connect", "acct-1", "acct-2")])[0] == []
