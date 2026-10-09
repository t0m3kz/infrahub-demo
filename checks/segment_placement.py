"""Validate segment placement and VXLAN stretch intent consistency."""

from __future__ import annotations

from typing import Any

from infrahub_sdk.checks import InfrahubCheck

from utils.data_cleaning import clean_data


class CheckSegmentPlacement(InfrahubCheck):
    """Validate deployment-type compatibility and VXLAN stretch constraints."""

    query = "segment_placement"

    _VLAN_ALLOWED_DEPLOYMENTS = {
        "TopologyCustomerDC",
        "TopologyCustomerColocation",
        "TopologyCustomerOffice",
    }
    _VXLAN_ALLOWED_DEPLOYMENTS = {
        "TopologyCustomerDC",
        "TopologyCustomerColocation",
    }
    _VXLAN_ALLOWED_PARENTS = {
        "TopologyDataCenter",
        "TopologyColocationMetro",
    }

    # HA pairs that can be a segment's gateway (terminate_inline): the fabric
    # renders such a segment pure L2, so nothing else would route it.
    _INLINE_SERVICE_KINDS = {
        "ManagedFirewallHA",
        "ManagedLoadbalancerHA",
        "ManagedProxyHA",
    }

    def validate(self, data: Any) -> None:
        cleaned = clean_data(data)
        self._validate_vlan_segments(cleaned.get("ManagedVlanSegment") or [])
        self._validate_vxlan_segments(cleaned.get("ManagedVxlanSegment") or [])
        self._validate_cloud_segments(cleaned.get("CloudNetworkSegment") or [])
        for segment in [*(cleaned.get("ManagedVlanSegment") or []), *(cleaned.get("ManagedVxlanSegment") or [])]:
            self._validate_inline_termination(segment)

    def _validate_inline_termination(self, segment: dict[str, Any]) -> None:
        """terminate_inline needs an on-prem HA pair as inline_service: leaves
        and border leaves drop the segment's gateway, so without one the
        segment has no gateway anywhere."""
        if not segment.get("terminate_inline"):
            return
        seg_name = segment.get("name", "<unnamed-segment>")
        inline_service = segment.get("inline_service")
        if not isinstance(inline_service, dict):
            self.log_error(
                message=(
                    f"Segment '{seg_name}' has terminate_inline=true but no inline_service — "
                    "the fabric renders it pure L2, so it has no gateway at all"
                )
            )
            return
        kind = inline_service.get("typename")
        if kind not in self._INLINE_SERVICE_KINDS:
            self.log_error(
                message=(
                    f"Segment '{seg_name}' inline_service '{inline_service.get('name', '<unknown>')}' is a "
                    f"'{kind}', which cannot be its gateway. Allowed: {sorted(self._INLINE_SERVICE_KINDS)}"
                )
            )

    def _validate_vlan_segments(self, segments: list[dict[str, Any]]) -> None:
        for segment in segments:
            seg_name = segment.get("name", "<unnamed-vlan-segment>")
            deployment = segment.get("customer_deployment")
            if not isinstance(deployment, dict):
                # A segment with no deployment terminates nowhere: no leaf
                # carries it and no firewall context serves it.
                self.log_error(
                    message=f"VLAN segment '{seg_name}' has no customer_deployment. Attach it to its deployment."
                )
                continue

            dep_kind = deployment.get("typename")
            dep_name = deployment.get("name", "<unknown>")
            if dep_kind not in self._VLAN_ALLOWED_DEPLOYMENTS:
                self.log_error(
                    message=(
                        f"VLAN segment '{seg_name}' is attached to unsupported deployment type "
                        f"'{dep_kind}' ({dep_name}). Allowed: "
                        f"{sorted(self._VLAN_ALLOWED_DEPLOYMENTS)}"
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
