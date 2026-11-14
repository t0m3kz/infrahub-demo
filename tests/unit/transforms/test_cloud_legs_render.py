"""The NX-OS edge renders a cloud leg like any external session: its far end is a CloudPeeringInterface, not a device."""

from __future__ import annotations

from typing import Any

from tests.unit.cloud_leg_helpers import attachment, edges, peering, prefix
from tests.unit.transforms.test_external_vrf_peers_render import (
    _bgp_node,
    _fixture,
    _neighbor_block,
    _prefix_list,
    _run_edge,
)

_AWS = "VRF-PROD-EQXFR2-AWS-EUC1"
_LOCAL = ("dc1-edge-01", "Ethernet1/13.1000")


def _cloud_fixture(**peering_kwargs: Any) -> dict[str, Any]:
    """The external-peers fixture with its PROD AWS session turned into a cloud leg."""
    data = _fixture()
    (peering_node,) = [e["node"] for e in _bgp_node(data)["peerings"]["edges"] if e["node"]["name"]["value"] == _AWS]
    peering_node["capabilities"]["edges"].pop()
    peering_node["bgp_processes"]["edges"].pop()
    peering_node["password"] = {"node": None}
    data["CloudPeeringInterface"] = edges(peering("vif-nordix", "dx-nordix", ends=(_LOCAL,), **peering_kwargs))
    data["CloudGatewayAttachment"] = edges(
        attachment("tgwa-dxgw", "tgw", peer="dxgw", table="shared"),
        attachment(
            "tgwa-vpc-nordix",
            "tgw",
            network="nordix-vpc-prod",
            blocks=(prefix("10.10.0.0/16"),),
            propagates=("shared",),
        ),
    )
    return data


class TestCloudLegRender:
    """Rule 10 of 30_all: the AWS session accepts C001's VPC block and is announced C001's on-prem space."""

    def test_the_session_has_the_clouds_neighbor_asn_and_key(self) -> None:
        """peer_ip, peer_asn and auth of the peering interface, in the PROD VRF, external."""
        output = _run_edge(_cloud_fixture())
        block = _neighbor_block(output, "10.255.5.6")
        assert "      remote-as 64512\n" in block
        assert "        route-map RM-VRF-PROD-EQXFR2-AWS-EUC1-IN in\n" in block
        assert "        route-map RM-VRF-PROD-EQXFR2-AWS-EUC1-OUT out\n" in block
        assert "s3cret" in block

    def test_in_accepts_the_vpc_block_and_out_announces_the_owners_space(self) -> None:
        """IN: the block behind the gateway chain; OUT: C001's PROD space, never another owner's, minus IN."""
        output = _run_edge(_cloud_fixture())
        assert _prefix_list(output, "PL-VRF-PROD-EQXFR2-AWS-EUC1-IN") == ["10.10.0.0/16"]
        assert "! VRF-PROD-EQXFR2-AWS-EUC1: owners C001\n" in output
        assert "route-map RM-VRF-PROD-EQXFR2-AWS-EUC1-IN permit 10\n" in output
        out = _prefix_list(output, "PL-VRF-PROD-EQXFR2-AWS-EUC1-OUT")
        assert "10.10.0.0/16" not in out
        assert "198.18.0.0/24" in out  # C001's partner tunnel range, registered behind the SD-WAN session
        assert "10.176.5.0/24" not in out  # C005's, not C001's

    def test_the_sd_wan_session_is_unchanged(self) -> None:
        """The SD-WAN gateway still accepts what its offices and tunnels register."""
        output = _run_edge(_cloud_fixture())
        assert _prefix_list(output, "PL-VRF-PROD-EQXFR2-SDWAN-IN")[-1] == "198.18.0.0/24"

    def test_a_provider_owned_account_is_deny_all(self) -> None:
        """Only a customer owner counts: the session renders, accepting and announcing nothing."""
        output = _run_edge(_cloud_fixture(account_owner="P004", account_owner_type="OrganizationProvider"))
        assert (
            "! VRF-PROD-EQXFR2-AWS-EUC1: no registered customer owner, announces nothing and accepts nothing\n"
            in output
        )
        assert _prefix_list(output, "PL-VRF-PROD-EQXFR2-AWS-EUC1-IN") == []

    def test_a_public_peering_announces_what_it_advertises(self) -> None:
        """advertised_prefixes go out on that session alone and nothing is accepted."""
        output = _run_edge(_cloud_fixture(scope="public", advertised=(prefix("198.51.100.0/24"),)))
        assert _prefix_list(output, "PL-VRF-PROD-EQXFR2-AWS-EUC1-IN") == []
        assert _prefix_list(output, "PL-VRF-PROD-EQXFR2-AWS-EUC1-OUT") == ["198.51.100.0/24"]
