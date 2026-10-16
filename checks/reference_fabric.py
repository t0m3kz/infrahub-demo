"""Which data centers are a reference fabric, and every segment that needs group policy sits on one."""

from __future__ import annotations

from typing import Any

from transforms.helpers.vxlan import _is_multisite_segment
from utils.data_cleaning import clean_data
from utils.exchange_transit import is_routed_tenant_segment
from utils.group_policy import (
    gbp_required,
    profiles_by_platform,
    segment_sites,
    site_kind,
    site_reason,
    tags_by_segment,
    vteps_by_data_center,
)

from .base import WarningCheck

VXLAN_KIND = "ManagedVxlanSegment"
NOT_A_DATA_CENTER = "not a data center"
"""The reason of a segment's site that is no TopologyDataCenter (never a reference fabric)."""


class CheckReferenceFabric(WarningCheck):
    """Report each data center against the reference fabric profile, and fail the segments that need it.

    Targetless: the profile rows, a segment's sites and a site's VTEPs span the fleet.

    Profile rows (error): a platform without exactly one usable
    DcimGroupPolicyProfile row has no reference fabric (utils/group_policy.profiles_by_platform).

    Data centers (info, never blocking): ``DC10 is a reference fabric (vxlan_gpo, N VTEPs)``
    or ``DC11 is not a reference fabric: <reason>`` (utils/group_policy.site_reason).
    A site that is not one keeps L0 only (SVI ACL, PBR).

    Segments:

    - a routed tenant segment (utils/exchange_transit.is_routed_tenant_segment)
      active on a reference fabric without its group tag (error): its hosts
      would be unclassified, outside every contract;
    - one that needs component group policy (utils/group_policy.gbp_required)
      and is VLAN-only cannot carry a group tag: host-enforced (warning);
    - one that needs component group policy is active in a single site
      (error), and that site is a reference fabric (error: its intra-segment
      flows would have no enforcement point);
    - any other tagged segment (segment or component tags) stretched between a
      reference and a non-reference site is enforced on the reference side
      only (warning): the other side arrives as the policy-unaware group.
    """

    query = "reference_fabric"

    def validate(self, data: Any) -> None:
        cleaned = clean_data(data)
        profiles, problems = profiles_by_platform(cleaned.get("DcimGroupPolicyProfile") or [])
        for problem in problems:
            self.log_error(message=f"Reference fabric profile: {problem}")
        reasons = self._site_reasons(cleaned, profiles)

        tags_of = tags_by_segment(cleaned.get("SecurityTag") or [])
        names = {str(dc["id"]): str(dc.get("name") or dc["id"]) for dc in cleaned.get("TopologyDataCenter") or []}
        for segment in sorted(cleaned.get("ManagedNetworkSegment") or [], key=lambda item: str(item.get("name"))):
            tag_count = len(tags_of.get(str(segment.get("id")), []))
            sites = self._sites(segment, names)
            self._validate_segment_tag(segment, sites, reasons)
            if gbp_required(segment, tag_count):
                self._validate_required(segment, sites, reasons)
            elif tag_count or segment.get("security_tag"):
                self._validate_stretch(segment, sites, reasons)

    def _site_reasons(self, cleaned: dict[str, Any], profiles: dict[str, dict[str, Any]]) -> dict[str, str | None]:
        """Site id -> site_reason (None: reference fabric), one info per data center by name.

        A pod's VTEPs belong to the pod's data center.
        """
        vteps_of = vteps_by_data_center(cleaned.get("TopologyDataCenter") or [], cleaned.get("TopologyPod") or [])
        reasons: dict[str, str | None] = {}
        for data_center in sorted(cleaned.get("TopologyDataCenter") or [], key=lambda item: str(item.get("name"))):
            site_id = str(data_center.get("id"))
            vteps = vteps_of.get(site_id, [])
            reason = reasons[site_id] = site_reason(vteps, profiles)
            name = data_center.get("name", site_id)
            if reason is None:
                kind = site_kind(vteps, profiles)
                self.log_info(message=f"{name} is a reference fabric ({kind}, {len(vteps)} VTEPs)")
            else:
                self.log_info(message=f"{name} is not a reference fabric: {reason}")
        return reasons

    @staticmethod
    def _sites(segment: dict[str, Any], names: dict[str, str]) -> list[tuple[str, str]]:
        """(id, name) of every site the segment is active in (utils/group_policy.segment_sites), sorted by name.

        A data center is named by the query; another site by its deployment, else by its id.
        """
        for activation in segment.get("segment_deployments") or []:
            deployment = activation.get("deployment") or {}
            if deployment.get("id"):
                names = {str(deployment["id"]): str(deployment.get("name") or deployment["id"]), **names}
        return sorted(((site, names.get(site, site)) for site in segment_sites(segment)), key=lambda site: site[1])

    def _validate_segment_tag(
        self, segment: dict[str, Any], sites: list[tuple[str, str]], reasons: dict[str, str | None]
    ) -> None:
        """A routed tenant segment on a reference fabric must carry its group tag."""
        if segment.get("security_tag") or not is_routed_tenant_segment(segment):
            return
        reference = [site_name for site_id, site_name in sites if reasons.get(site_id, NOT_A_DATA_CENTER) is None]
        if reference:
            self.log_error(
                message=(
                    f"Segment '{segment.get('name', segment.get('id'))}' is a routed tenant segment on reference "
                    f"fabric {', '.join(reference)} without a group tag: its hosts would be unclassified"
                )
            )

    def _validate_required(
        self, segment: dict[str, Any], sites: list[tuple[str, str]], reasons: dict[str, str | None]
    ) -> None:
        """A segment that needs group policy: VLAN-only warns, VXLAN must be single-site on a reference fabric."""
        name = segment.get("name", segment.get("id"))
        if segment.get("typename") != VXLAN_KIND:
            self.log_warning(
                message=(
                    f"segment '{name}' needs group policy (two or more tagged components) but is "
                    "VLAN-only: its intra-segment flows are not network-enforceable (host-enforced)"
                )
            )
            return
        if _is_multisite_segment(segment):
            self.log_error(
                message=f"Segment '{name}' needs group policy but is active in more than one site; it must be single-site"
            )
            return
        if not sites:
            self.log_error(message=f"Segment '{name}' needs group policy but is active in no site")
        for site_id, site_name in sites:
            reason = reasons.get(site_id, NOT_A_DATA_CENTER)
            if reason is not None:
                self.log_error(
                    message=(
                        f"Segment '{name}' needs group policy, but {site_name} is not a reference fabric "
                        f"({reason}): its intra-segment flows have no enforcement point"
                    )
                )

    def _validate_stretch(
        self, segment: dict[str, Any], sites: list[tuple[str, str]], reasons: dict[str, str | None]
    ) -> None:
        """A tagged segment active on both a reference and a non-reference site is enforced on one side only."""
        reference = [site_name for site_id, site_name in sites if reasons.get(site_id, NOT_A_DATA_CENTER) is None]
        other = [site_name for site_id, site_name in sites if reasons.get(site_id, NOT_A_DATA_CENTER) is not None]
        if reference and other:
            self.log_warning(
                message=(
                    f"segment '{segment.get('name', segment.get('id'))}' is stretched between reference "
                    f"fabric {', '.join(reference)} and non-reference {', '.join(other)}: group policy is enforced "
                    "on the reference side only"
                )
            )
