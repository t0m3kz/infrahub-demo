"""Validate segment placement and VXLAN stretch intent consistency."""

from __future__ import annotations

from ipaddress import ip_interface, ip_network
from typing import Any

from utils.data_cleaning import clean_data
from utils.exchange_transit import is_exchange_zone

from .base import WarningCheck

OFFICE_ENVIRONMENT = "p"
"""An office is always PROD: it has no environment of its own, its segments must be `p`."""


class CheckSegmentPlacement(WarningCheck):
    """Validate deployment-type compatibility and VXLAN stretch constraints.

    A segment agrees with every footprint it names. Its environment must
    be the footprint's (an ERROR: the environment picks the segment's
    namespace, zone and firewall leg, so a segment in another environment than
    its footprint lands in the wrong VRF): DC and Colocation footprints state
    one, an office is always PROD (`p`), and a cloud footprint has none (the
    segment's environment against its CloudAccount's is
    checks/routing_invariants.py's). The owner is a WARNING when it differs
    from the footprint's: an exchange-zone segment is a shared service of
    another owner by design, and a footprint owned by a provider hosts
    customers' segments.
    """

    query = "segment_placement"

    # Footprints that state the environment they represent (TopologyEnvironmentScoped).
    _ENVIRONMENT_SCOPED_DEPLOYMENTS = {"TopologyCustomerDC", "TopologyCustomerColocation"}
    _VXLAN_ALLOWED_DEPLOYMENTS = {
        "TopologyCustomerDC",
        "TopologyCustomerColocation",
        "TopologyCustomerOffice",
    }
    # Footprints that are TopologySegmentHosting themselves: they host their
    # segments (VNI pool, SegmentDeployment) and have no hosting parent to check.
    # An office has no border gateway to stretch a segment through, so its
    # segments are local-only.
    _SELF_HOSTING_DEPLOYMENTS = {"TopologyCustomerOffice"}
    _VXLAN_ALLOWED_PARENTS = {
        "TopologyDataCenter",
        "TopologyColocationMetro",
    }

    def validate(self, data: Any) -> None:
        cleaned = clean_data(data)
        self._validate_vxlan_segments(cleaned.get("ManagedVxlanSegment") or [])
        self._validate_cloud_segments(cleaned.get("CloudNetworkSegment") or [])
        for segment in cleaned.get("ManagedVxlanSegment") or []:
            self._validate_deployment_agreement(segment, segment.get("customer_deployments") or [])
            self._validate_termination(segment)

    def _validate_deployment_agreement(self, segment: dict[str, Any], deployments: list[dict[str, Any]]) -> None:
        """The segment's environment and owner against each footprint it names."""
        seg_name = segment.get("name", "<unnamed-segment>")
        seg_environment = segment.get("environment")
        seg_owner = segment.get("owner") or {}
        shared_service = is_exchange_zone((segment.get("security_zone") or {}).get("name"))
        for deployment in deployments:
            dep_kind = deployment.get("typename")
            dep_name = deployment.get("name", "<unknown>")
            if dep_kind in self._ENVIRONMENT_SCOPED_DEPLOYMENTS:
                expected = deployment.get("environment")
            elif dep_kind == "TopologyCustomerOffice":
                expected = OFFICE_ENVIRONMENT
            else:
                continue
            if expected and seg_environment != expected:
                self.log_error(
                    message=(
                        f"Segment '{seg_name}' is environment '{seg_environment}' but {dep_kind} '{dep_name}' is "
                        f"environment '{expected}': a segment shares the environment of every footprint it names"
                    )
                )
            dep_owner = deployment.get("owner") or {}
            if (
                not shared_service
                and dep_owner.get("typename") != "OrganizationProvider"
                and dep_owner.get("id") != seg_owner.get("id")
            ):
                self.log_warning(
                    message=(
                        f"Segment '{seg_name}' is owned by '{seg_owner.get('org_id')}' but {dep_kind} '{dep_name}' "
                        f"is owned by '{dep_owner.get('org_id')}'"
                    )
                )

    def _validate_termination(self, segment: dict[str, Any]) -> None:
        """A segment terminated on a firewall context (``terminated_on``) is a floating VIP the pair owns.

        - its ``gateway`` is set and inside the segment's prefix (the VIP the firewall transform renders);
        - every footprint it names is a DC footprint (a colocation firewall has no trunk of its own to the leaf, an
          office or a cloud has no firewall pair of ours);
        - the context serves every one of those footprints (``served_deployments``), otherwise the segment would be
          bridged to a pair that holds none of its rules.

        The context is a firewall context by schema (``terminated_on`` peers ``ManagedFirewallContext`` only).
        """
        context = segment.get("terminated_on")
        if not context:
            return
        seg_name = segment.get("name", "<unnamed-segment>")
        context_name = context.get("name", "<unknown>")
        gateway = (segment.get("gateway") or {}).get("address")
        prefix = ((segment.get("gateway") or {}).get("ip_prefix") or {}).get("prefix")
        if not gateway or not prefix:
            self.log_error(
                message=(
                    f"Segment '{seg_name}' is terminated on context '{context_name}' but has no gateway: "
                    "the gateway is the VIP of the firewall pair"
                )
            )
        elif ip_interface(gateway).ip not in ip_network(prefix, strict=False):
            self.log_error(
                message=(
                    f"Segment '{seg_name}' gateway {gateway} (the VIP of context '{context_name}') is outside "
                    f"the segment prefix {prefix}"
                )
            )
        served = {deployment.get("id") for deployment in context.get("served_deployments") or []}
        for deployment in segment.get("customer_deployments") or []:
            dep_kind = deployment.get("typename")
            dep_name = deployment.get("name", "<unknown>")
            if dep_kind != "TopologyCustomerDC":
                self.log_error(
                    message=(
                        f"Segment '{seg_name}' is terminated on context '{context_name}' but names {dep_kind} "
                        f"'{dep_name}': only a TopologyCustomerDC can be terminated on a firewall context"
                    )
                )
            elif deployment.get("id") not in served:
                self.log_error(
                    message=(
                        f"Segment '{seg_name}' is terminated on context '{context_name}' but TopologyCustomerDC "
                        f"'{dep_name}' is not served by it"
                    )
                )

    def _validate_vxlan_segments(self, segments: list[dict[str, Any]]) -> None:
        for segment in segments:
            seg_name = segment.get("name", "<unnamed-vxlan-segment>")
            stretch_scope = segment.get("stretch_scope") or "local"
            customer_deployments = segment.get("customer_deployments") or []

            if not customer_deployments:
                self.log_error(
                    message=(
                        f"VXLAN segment '{seg_name}' has no customer_deployments. "
                        "Attach at least one supported deployment footprint."
                    )
                )
                continue

            if stretch_scope == "local" and len(customer_deployments) != 1:
                self.log_error(
                    message=(
                        f"VXLAN segment '{seg_name}' uses stretch_scope=local but has "
                        f"{len(customer_deployments)} deployments. Exactly one is required."
                    )
                )

            if stretch_scope in {"metro", "dc_pair", "global"} and len(customer_deployments) < 2:
                self.log_error(
                    message=(
                        f"VXLAN segment '{seg_name}' uses stretch_scope={stretch_scope} but has "
                        f"{len(customer_deployments)} deployment(s). At least two are required."
                    )
                )

            for deployment in customer_deployments:
                dep_kind = deployment.get("typename")
                dep_name = deployment.get("name", "<unknown>")
                if dep_kind not in self._VXLAN_ALLOWED_DEPLOYMENTS:
                    self.log_error(
                        message=(
                            f"VXLAN segment '{seg_name}' is attached to unsupported deployment type "
                            f"'{dep_kind}' ({dep_name}). Allowed: "
                            f"{sorted(self._VXLAN_ALLOWED_DEPLOYMENTS)}"
                        )
                    )

                if dep_kind in self._SELF_HOSTING_DEPLOYMENTS:
                    if stretch_scope != "local":
                        self.log_error(
                            message=(
                                f"VXLAN segment '{seg_name}' uses stretch_scope={stretch_scope} but is attached to "
                                f"{dep_kind} '{dep_name}': an office has no border gateway to stretch a segment "
                                "through, so its segments must be local"
                            )
                        )
                    continue

                parent = deployment.get("parent") or {}
                parent_kind = parent.get("typename")
                parent_name = parent.get("name", "<unknown>")
                if parent_kind and parent_kind not in self._VXLAN_ALLOWED_PARENTS:
                    self.log_error(
                        message=(
                            f"VXLAN segment '{seg_name}' deployment '{dep_name}' resolves to parent "
                            f"'{parent_kind}' ({parent_name}), which is not TopologySegmentHosting."
                        )
                    )

    def _validate_cloud_segments(self, segments: list[dict[str, Any]]) -> None:
        for segment in segments:
            seg_name = segment.get("name", "<unnamed-cloud-segment>")
            deployment = segment.get("customer_deployment")
            if not isinstance(deployment, dict):
                self.log_error(
                    message=f"Cloud segment '{seg_name}' has no customer_deployment. Attach it to its deployment."
                )
                continue

            dep_kind = deployment.get("typename")
            dep_name = deployment.get("name", "<unknown>")
            if dep_kind != "TopologyCustomerCloud":
                self.log_error(
                    message=(
                        f"Cloud segment '{seg_name}' is attached to '{dep_kind}' ({dep_name}). "
                        "Cloud segments must attach only to TopologyCustomerCloud."
                    )
                )
