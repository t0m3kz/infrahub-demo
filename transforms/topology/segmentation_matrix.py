"""Per data center segmentation matrix: component group tags, their contracts and the reference fabric verdict."""

from __future__ import annotations

from typing import Any

from infrahub_sdk.transforms import InfrahubTransform

from transforms.helpers.acl import _leaf_enforces
from transforms.helpers.group_policy import get_group_contracts
from utils.data_cleaning import clean_data
from utils.group_policy import (
    fabric_reason,
    gbp_required,
    profiles_by_platform,
    site_kind,
    site_reason,
    site_vteps,
    tags_by_segment,
)


class SegmentationMatrix(InfrahubTransform):
    """The vendor-neutral segmentation of one data center, as JSON.

    - ``site`` / ``vteps``: whether the DC is a reference fabric and the one
      group-policy kind of its VTEPs (utils/group_policy.site_reason /
      site_kind), each VTEP with why it is outside its platform's profile row
      (fabric_reason; null: in it);
    - ``tags``: the component group tags of the DC's segments;
    - ``segments``: every segment deployed in the DC, whether it needs group
      policy and whether its leaf SVI carries its ACL (_leaf_enforces, the L0
      decision, not re-derived);
    - ``contracts``: the forward contract rows of the segments that need group
      policy (transforms/helpers/group_policy.py).

    Nothing here is rendered on a device. Sorted and id-free, so a re-render
    of unchanged data is byte-identical.
    """

    query = "segmentation_matrix"

    async def transform(self, data: dict[str, Any]) -> dict[str, Any]:
        cleaned = clean_data(data)
        data_centers = cleaned.get("TopologyDataCenter") or []
        if not data_centers:
            raise ValueError("segmentation_matrix: the query returned no TopologyDataCenter")
        data_center = data_centers[0]

        profiles, _problems = profiles_by_platform(cleaned.get("DcimGroupPolicyProfile") or [])
        vteps = site_vteps(
            [
                *(data_center.get("devices") or []),
                *(device for pod in cleaned.get("TopologyPod") or [] for device in pod.get("devices") or []),
            ]
        )
        reason = site_reason(vteps, profiles)

        segments = self._segments(data_center)
        tags_of = tags_by_segment(cleaned.get("SecurityTag") or [])

        return {
            "deployment": data_center.get("name"),
            "site": {"reference": reason is None, "kind": site_kind(vteps, profiles), "reason": reason},
            "vteps": [self._vtep(device, profiles) for device in vteps],
            "tags": [
                self._tag(tag, segment)
                for segment in segments
                for tag in sorted(tags_of.get(segment["id"], []), key=lambda item: item.get("group_id") or 0)
            ],
            "segments": [
                {
                    "name": segment.get("name"),
                    "isolation_mode": segment.get("isolation_mode"),
                    "gbp_required": gbp_required(segment, len(tags_of.get(segment["id"], []))),
                    "leaf_svi_acl": _leaf_enforces(segment),
                    "inline_firewall": (segment.get("inline_service") or {}).get("name"),
                }
                for segment in segments
            ],
            "contracts": [
                row
                for segment in segments
                if gbp_required(segment, len(tags_of.get(segment["id"], [])))
                for row in get_group_contracts(segment, tags_of[segment["id"]])
            ],
        }

    @staticmethod
    def _segments(data_center: dict[str, Any]) -> list[dict[str, Any]]:
        """The DC's deployed segments, once each, by name."""
        by_id: dict[str, dict[str, Any]] = {}
        for deployment in data_center.get("segment_deployments") or []:
            segment = deployment.get("segment") or {}
            if segment.get("id"):
                by_id.setdefault(segment["id"], segment)
        return sorted(by_id.values(), key=lambda segment: str(segment.get("name")))

    @staticmethod
    def _vtep(device: dict[str, Any], profiles: dict[str, dict[str, Any]]) -> dict[str, Any]:
        return {
            "name": device.get("name"),
            "role": device.get("role"),
            "device_type": (device.get("device_type") or {}).get("name"),
            "release": (device.get("software_image") or {}).get("version"),
            "reason": fabric_reason(device, profiles),
        }

    @staticmethod
    def _tag(tag: dict[str, Any], segment: dict[str, Any]) -> dict[str, Any]:
        prefix = (segment.get("gateway") or {}).get("ip_prefix") or {}
        return {
            "name": tag.get("name"),
            "group_id": tag.get("group_id"),
            "component": ", ".join(sorted(str(item.get("fqdn")) for item in tag.get("components") or [])),
            "segment": segment.get("name"),
            "vrf": (prefix.get("ip_namespace") or {}).get("name"),
        }
