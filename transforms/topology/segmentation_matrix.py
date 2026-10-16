"""Per data center segmentation matrix: group tags, their classification and contracts, and the reference fabric verdict."""

from __future__ import annotations

from typing import Any

from infrahub_sdk.transforms import InfrahubTransform

from transforms.helpers.acl import _leaf_enforces
from transforms.helpers.group_policy import (
    DEFAULT_ACTION,
    REPLIES,
    Group,
    data_center_contracts,
    segment_security_view,
    segment_vrf,
    vxlan_gpo_view,
)
from utils.data_cleaning import clean_data
from utils.exchange_transit import is_routed_tenant_segment
from utils.group_policy import (
    fabric_reason,
    gbp_required,
    reserved_groups,
    site_kind,
)
from utils.segment_addressing import classifier_prefix


class SegmentationMatrix(InfrahubTransform):
    """The vendor-neutral segmentation of one data center, as JSON.

    - ``site`` / ``vteps``: whether the DC is a reference fabric and the one
      group-policy kind of its VTEPs (utils/group_policy.site_reason /
      site_kind), each VTEP with why it is outside its platform's profile row
      (fabric_reason; null: in it);
    - ``site.enforce``: the data center's staging switch (``group_policy_enforce``): False means a
      reference fabric configures the contracts but its devices do not enforce them;
    - ``segments``: every segment active in the DC, with its group, customer
      (the segment owner: a label, not a tag), VRF, whether it needs
      component group policy and whether its leaf SVI carries its ACL
      (_leaf_enforces, the L0 decision, not re-derived);
    - ``groups``: the segment and component groups of those segments;
    - ``classification``: how a segment group's hosts are matched (VLAN ID
      of a VLAN segment, VNI of a VXLAN segment, gateway prefix), and the
      routed tenant segments with no tag yet (unclassified: the
      reference_fabric check fails such a segment on a reference fabric);
    - ``contracts``: the fabric's permit rows with at least one of those
      groups (transforms/helpers/group_policy.get_vrf_contracts), deny by
      default;
    - ``kinds``: the same contracts per group-policy kind the profile rows
      name (vxlan_gpo: group IDs, reserved groups, the policy-unaware rule;
      segment_security: segments by prefix and policies), as this DC would
      enforce them once it is a reference fabric, with every other site as
      its VTEPs are.

    Nothing here is rendered on a device. Sorted and id-free, so a re-render
    of unchanged data is byte-identical.
    """

    query = "segmentation_matrix"

    async def transform(self, data: dict[str, Any]) -> dict[str, Any]:
        cleaned = clean_data(data)
        targets = cleaned.get("target") or []
        if not targets:
            raise ValueError("segmentation_matrix: the query returned no TopologyDataCenter")
        data_center = targets[0]
        dc_id = str(data_center.get("id"))

        contracts = data_center_contracts(cleaned, dc_id)
        profiles, vteps = contracts.profiles, contracts.vteps
        # As this data center would enforce once it is a reference fabric.
        reference_sites = contracts.reference_sites | {dc_id}
        components_of = contracts.components_of

        views = {
            "segment_security": segment_security_view(
                contracts.all_groups,
                contracts.rows,
                {str(segment.get("name")): classifier_prefix(segment) for segment in contracts.all_segments},
                local=contracts.local_names,
            ),
            "vxlan_gpo": vxlan_gpo_view(
                contracts.all_groups,
                contracts.rows,
                reserved=reserved_groups(profiles, "vxlan_gpo"),
                reference_sites=reference_sites,
                local=contracts.local_names,
            ),
        }
        kinds = sorted({str(profile["group_policy_kind"]) for profile in profiles.values()} & set(views))

        return {
            "deployment": data_center.get("name"),
            "site": {
                "reference": contracts.reason is None,
                "kind": site_kind(vteps, profiles),
                "reason": contracts.reason,
                "enforce": contracts.enforce,
            },
            "vteps": [self._vtep(device, profiles) for device in vteps],
            "segments": [
                self._segment(segment, contracts.by_segment, len(components_of.get(str(segment.get("id")), [])))
                for segment in contracts.segments
            ],
            "groups": [group.as_dict() for group in contracts.local],
            "classification": self._classification(contracts.segments, contracts.by_segment, dc_id),
            "contracts": {"default_action": DEFAULT_ACTION, "replies": REPLIES, "rows": contracts.rows},
            "kinds": {kind: views[kind] for kind in kinds},
        }

    @staticmethod
    def _segment(segment: dict[str, Any], by_segment: dict[str, list[Group]], tag_count: int) -> dict[str, Any]:
        groups = by_segment.get(str(segment.get("id"))) or []
        return {
            "name": segment.get("name"),
            "group": groups[0].name if groups else None,
            "customer": (segment.get("owner") or {}).get("org_id"),
            "vrf": segment_vrf(segment),
            "isolation_mode": segment.get("isolation_mode"),
            "gbp_required": gbp_required(segment, tag_count),
            "leaf_svi_acl": _leaf_enforces(segment),
            "inline_firewall": (segment.get("inline_service") or {}).get("name"),
        }

    @staticmethod
    def _classification(
        segments: list[dict[str, Any]], by_segment: dict[str, list[Group]], dc_id: str
    ) -> dict[str, Any]:
        """Each segment group's selector here, and the routed tenant segments with no group."""
        selectors = []
        for segment in segments:
            groups = by_segment.get(str(segment.get("id")))
            if not groups:
                continue
            vnis = sorted(
                {
                    activation.get("vni")
                    for activation in segment.get("segment_deployments") or []
                    if (activation.get("deployment") or {}).get("id") == dc_id and activation.get("vni")
                }
            )
            selectors.append(
                {
                    "group": groups[0].name,
                    "segment": segment.get("name"),
                    "vrf": groups[0].vrf,
                    "vlan_id": segment.get("vlan_id"),
                    "vni": vnis[0] if vnis else None,
                    "prefix": classifier_prefix(segment),
                }
            )
        return {
            "selectors": selectors,
            "unclassified": [
                segment.get("name")
                for segment in segments
                if is_routed_tenant_segment(segment) and str(segment.get("id")) not in by_segment
            ],
        }

    @staticmethod
    def _vtep(device: dict[str, Any], profiles: dict[str, dict[str, Any]]) -> dict[str, Any]:
        return {
            "name": device.get("name"),
            "role": device.get("role"),
            "device_type": (device.get("device_type") or {}).get("name"),
            "release": (device.get("software_image") or {}).get("version"),
            "reason": fabric_reason(device, profiles),
        }
