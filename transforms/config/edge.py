from typing import Any, ClassVar

from transforms.common import BaseDeviceTransform
from transforms.helpers.bgp_cloud import cloud_legs, graft_cloud_far_ends, legs_by_interface
from transforms.helpers.bgp_external import (
    get_external_peer_prefixes,
    get_external_session_policies,
    get_internet_prefixes,
    get_onprem_prefixes,
)
from transforms.helpers.vrf import plain_vrf_names
from utils.circuit_underlay import SESSION_CIRCUIT
from utils.routing_identity import customer_tags, identity_in_use, routing_community_regexes


class Edge(BaseDeviceTransform):
    query = "edge_config"
    template_subdir = "edges"
    # A VTEP only where the edge is its colocation site's EVPN Multi-Site border
    # gateway: get_vxlan_config returns None without stretched-segment
    # activations, so every other edge renders exactly as before.
    device_role = "edge"
    # The shared firewall context's tenant 0/0 -> VIP stays a local static: each
    # edge resolves the VIP over the transit L2VNI, and exporting it as type-5
    # would let a DC import another site's PROD default via a stretched segment.
    export_tenant_default_to_evpn = False
    # Only edges/cisco_nxos.j2 defines the RM-<session>-IN/-OUT maps of an external session.
    external_policy_platforms: ClassVar[frozenset[str]] = frozenset({"cisco_nxos"})

    @staticmethod
    def _cloud_legs(device_name: str, extra_roots: dict | None) -> dict[str, Any]:
        """This device's cloud legs by interface name: the cloud far end of the sessions it terminates."""
        roots = extra_roots or {}
        return legs_by_interface(
            cloud_legs(roots.get("CloudPeeringInterface"), roots.get("CloudGatewayAttachment")), device_name
        )

    def prepared_device(self, data: Any) -> tuple[dict, dict, str | None]:
        """The prepared device, with the cloud peering interface of each cloud leg grafted on as its peering's far end."""
        device_data, extra_roots, platform_name = super().prepared_device(data)
        graft_cloud_far_ends(device_data, self._cloud_legs(device_data.get("name") or "", extra_roots))
        return device_data, extra_roots, platform_name

    def _extra_config(self, data: dict, platform_name: str, extra_roots: dict | None = None) -> dict[str, Any]:
        config = super()._extra_config(data, platform_name, extra_roots=extra_roots)
        roots = extra_roots or {}
        tags = customer_tags(roots.get("OrganizationCustomer"))
        # The public space an upstream transit session may announce.
        config["internet_prefixes"] = get_internet_prefixes(roots.get("IpamPrefix"))
        # External tenant-VRF peers (session.external): per handoff sub-interface, the owners behind
        # it, what it may send in and what it is announced out: the same owners' on-prem prefixes and
        # what they registered behind another external session, never another owner's, never back
        # to the session it was learned on (utils/ownership.py exchange_policies).
        legs = self._cloud_legs(data.get("name") or "", roots)
        config["external_policies"] = get_external_session_policies(
            data.get("interfaces"),
            get_external_peer_prefixes(
                data.get("interfaces"),
                data.get("name") or "",
                device_capabilities=data.get("capabilities"),
                offices=roots.get("TopologyCustomerOffice"),
                tunnels=roots.get(SESSION_CIRCUIT),
                cloud_legs=legs,
            ),
            get_onprem_prefixes(roots.get("ManagedNetworkSegment")),
            device_name=data.get("name") or "",
            customer_tags=tags,
            # Any tag in the model: what an external session accepts gets the owner's customer community, and
            # our communities never leave (utils/routing_identity.py).
            routing_identity=identity_in_use(tags, roots.get("ManagedNetworkSegment")),
            cloud_legs=legs,
        )
        # The expressions of the community-list that selects our communities (edges/cisco_nxos.j2).
        config["routing_community_regexes"] = routing_community_regexes()
        return config

    def _template_context(self, device_data: dict, platform_name: str, extra_roots: dict) -> dict:
        config = super()._template_context(device_data, platform_name, extra_roots)
        # The VRF instances with no L3 VNI: a plain `vrf context` (edges/cisco_nxos.j2).
        config["plain_vrfs"] = plain_vrf_names(device_data, config.get("bgp"), config.get("vxlan"))
        return config
