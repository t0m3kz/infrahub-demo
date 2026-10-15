"""Which data centers are a reference fabric, and every segment that needs group policy sits on one."""

from __future__ import annotations

from typing import Any

from infrahub_sdk.checks import InfrahubCheck

from transforms.helpers.vxlan import _is_multisite_segment
from utils.data_cleaning import clean_data
from utils.group_policy import (
    gbp_required,
    profiles_by_platform,
    site_kind,
    site_reason,
    site_vteps,
    tags_by_segment,
)

VXLAN_KIND = "ManagedVxlanSegment"
NOT_A_DATA_CENTER = "not a data center"
"""The reason of a segment's site that is no TopologyDataCenter (never a reference fabric)."""


class CheckReferenceFabric(InfrahubCheck):
    """Report each data center against the reference fabric profile, and fail the segments that need it.

    Targetless: the profile rows, a segment's sites and a site's VTEPs span the fleet.

    Profile rows (error): a platform without exactly one usable
    DcimGroupPolicyProfile row has no reference fabric (utils/group_policy.profiles_by_platform).

    Data centers (info, never blocking): ``DC10 is a reference fabric (vxlan_gpo, N VTEPs)``
    or ``DC11 is not a reference fabric: <reason>`` (utils/group_policy.site_reason).
    A site that is not one keeps L0 only (SVI ACL, PBR).

    Segments with tagged components (microsegmented):

    - one that needs group policy (utils/group_policy.gbp_required) and is
      VLAN-only cannot carry a group tag: host-enforced (warning);
    - one that needs group policy is active in a single site (error), and that
      site is a reference fabric (error: its intra-segment flows would have no
      enforcement point);
    - any other tagged segment stretched between a reference and a
      non-reference site is enforced on the reference side only (warning).
    """

    query = "reference_fabric"

    def validate(self, data: Any) -> None:
        cleaned = clean_data(data)
        profiles, problems = profiles_by_platform(cleaned.get("DcimGroupPolicyProfile") or [])
        for problem in problems:
            self.log_error(message=f"Reference fabric profile: {problem}")
        reasons = self._site_reasons(cleaned, profiles)

        tags_of = tags_by_segment(cleaned.get("SecurityTag") or [])
        for segment in sorted(cleaned.get("ManagedNetworkSegment") or [], key=lambda item: str(item.get("name"))):
            tag_count = len(tags_of.get(str(segment.get("id")), []))
            if gbp_required(segment, tag_count):
                self._validate_required(segment, reasons)
            elif tag_count:
                self._validate_stretch(segment, reasons)

    def _site_reasons(self, cleaned: dict[str, Any], profiles: dict[str, dict[str, Any]]) -> dict[str, str | None]:
        """Site id -> site_reason (None: reference fabric), one info per data center by name.

        A pod's VTEPs belong to the pod's data center.
        """
        pods: dict[str, list[dict[str, Any]]] = {}
        for pod in cleaned.get("TopologyPod") or []:
            if parent := (pod.get("parent") or {}).get("id"):
                pods.setdefault(str(parent), []).extend(pod.get("devices") or [])

        reasons: dict[str, str | None] = {}
        for data_center in sorted(cleaned.get("TopologyDataCenter") or [], key=lambda item: str(item.get("name"))):
            site_id = str(data_center.get("id"))
            vteps = site_vteps([*(data_center.get("devices") or []), *pods.get(site_id, [])])
            reason = reasons[site_id] = site_reason(vteps, profiles)
            name = data_center.get("name", site_id)
            if reason is None:
                kind = site_kind(vteps, profiles)
                self.log_info(message=f"{name} is a reference fabric ({kind}, {len(vteps)} VTEPs)")
            else:
                self.log_info(message=f"{name} is not a reference fabric: {reason}")
        return reasons

    @staticmethod
    def _sites(segment: dict[str, Any]) -> list[tuple[str, str]]:
        """(id, name) of every site the segment is active in, sorted by name."""
        sites = {
            (str(deployment["id"]), str(deployment.get("name") or deployment["id"]))
            for activation in segment.get("segment_deployments") or []
            if (deployment := activation.get("deployment") or {}).get("id")
        }
        return sorted(sites, key=lambda site: site[1])

    def _validate_required(self, segment: dict[str, Any], reasons: dict[str, str | None]) -> None:
        """A segment that needs group policy: VLAN-only warns, VXLAN must be single-site on a reference fabric."""
        name = segment.get("name", segment.get("id"))
        if segment.get("typename") != VXLAN_KIND:
            self.log_info(
                message=(
                    f"WARNING: segment '{name}' needs group policy (two or more tagged components) but is "
                    "VLAN-only: its intra-segment flows are not network-enforceable (host-enforced)"
                )
            )
            return
        if _is_multisite_segment(segment):
            self.log_error(
                message=f"Segment '{name}' needs group policy but is active in more than one site; it must be single-site"
            )
            return
        sites = self._sites(segment)
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

    def _validate_stretch(self, segment: dict[str, Any], reasons: dict[str, str | None]) -> None:
        """A tagged segment active on both a reference and a non-reference site is enforced on one side only."""
        sites = self._sites(segment)
        reference = [site_name for site_id, site_name in sites if reasons.get(site_id, NOT_A_DATA_CENTER) is None]
        other = [site_name for site_id, site_name in sites if reasons.get(site_id, NOT_A_DATA_CENTER) is not None]
        if reference and other:
            self.log_info(
                message=(
                    f"WARNING: segment '{segment.get('name', segment.get('id'))}' is stretched between reference "
                    f"fabric {', '.join(reference)} and non-reference {', '.join(other)}: group policy is enforced "
                    "on the reference side only"
                )
            )
