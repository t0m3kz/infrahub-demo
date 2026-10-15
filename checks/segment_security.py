"""Validate the security invariants every network segment kind must hold."""

from __future__ import annotations

from typing import Any

from infrahub_sdk.checks import InfrahubCheck

from transforms.helpers.group_policy import get_vrf_contracts, rule_groups, segment_groups
from transforms.helpers.policy import segment_flows
from utils.data_cleaning import clean_data
from utils.exchange_transit import (
    exchange_peer_zones,
    namespace_type_for_environment,
    zone_name_for_namespace_type,
)
from utils.group_policy import RESERVED_GROUPS, tag_pool
from utils.segment_addressing import segment_prefix

INTERNET_ZONE = zone_name_for_namespace_type("internet")
EXTERNAL_KIND = "ManagedExternalSegment"
CLOUD_KIND = "CloudNetworkSegment"


class CheckSegmentSecurity(InfrahubCheck):
    """Validate zone, address and identity of every ManagedNetworkSegment kind.

    Runs on settled data (gateways are allocated asynchronously, so a generator
    cannot refuse a rule to an address-less segment; this check does):

    - every segment has a security_zone;
    - a tenant segment's zone follows its environment (p -> PROD-ZONE, else
      NONPROD-ZONE); an External segment sits in an exchange zone;
    - computed segment names are unique across kinds;
    - a rule endpoint segment resolves to a prefix (or the rule has an explicit
      selector), unless it is the internet: otherwise it would render as any
      (a warning: L2-only segments have no gateway by design);
    - an app component's environment should match its segment's (warning);
    - every component of a microsegmented segment has a group tag; a tag groups
      exactly one component or exactly one segment, never both (an orphan is
      a warning); its group_id is in the band of the tag pool the
      group-policy profiles name, and no reserved group of a profile is;
    - a rule side carries a component tag only where its segment is
      microsegmented: inside it, or on a component dependency into or out of
      it;
    - every permit rule between two groups is a contract row of the group
      contract builder (transforms/helpers/group_policy.py): a rule the
      builder dropped would be denied by group policy while L0 permits it; a
      rule into a microsegmented segment that names no component (hand-written)
      opens every group of it (warning).
    """

    query = "segment_security"

    def validate(self, data: Any) -> None:
        cleaned = clean_data(data)
        segments = cleaned.get("ManagedNetworkSegment") or []
        rules = cleaned.get("SecurityPolicyRule") or []
        components = cleaned.get("AppComponent") or []
        tags = cleaned.get("SecurityTag") or []
        for segment in segments:
            self._validate_zone(segment)
        self._validate_unique_names(segments)
        self._validate_rule_endpoints(segments, rules)
        self._warn_component_environment(components)
        self._validate_component_tags(segments, components)
        self._validate_tags(tags, cleaned.get("DcimGroupPolicyProfile") or [])
        self._validate_rule_tags(rules, tags, segments)
        self._validate_contracts(segments, tags)

    def _validate_zone(self, segment: dict[str, Any]) -> None:
        """Zone present; tenant zone == f(environment); External in an exchange zone."""
        seg_name = segment.get("name", "<unnamed-segment>")
        kind = segment.get("typename")
        zone = (segment.get("security_zone") or {}).get("name")
        if not zone:
            self.log_error(
                message=(f"{kind} '{seg_name}' has no security_zone — zone-based firewalls match any zone on that side")
            )
            return
        if kind == EXTERNAL_KIND:
            if zone not in exchange_peer_zones():
                self.log_error(
                    message=(
                        f"External segment '{seg_name}' is in zone '{zone}', which is not an exchange zone. "
                        f"Allowed: {sorted(exchange_peer_zones())}"
                    )
                )
            return
        if zone in exchange_peer_zones():
            # A shared service segment (e.g. the DC-local MANAGEMENT services) sits in an exchange zone by design.
            return
        environment = segment.get("environment")
        if not environment:
            self.log_error(message=f"{kind} '{seg_name}' has no environment, so its zone '{zone}' cannot be verified")
            return
        expected = zone_name_for_namespace_type(namespace_type_for_environment(environment))
        if zone != expected:
            self.log_error(
                message=(
                    f"{kind} '{seg_name}' (environment '{environment}') is in zone '{zone}', expected '{expected}'"
                )
            )

    def _validate_unique_names(self, segments: list[dict[str, Any]]) -> None:
        """A computed name shared by two segments (of any kind) collides on policy names and HFIDs."""
        by_name: dict[str, list[str]] = {}
        for segment in segments:
            name = segment.get("name")
            if name:
                by_name.setdefault(name, []).append(str(segment.get("typename")))
        for name, kinds in sorted(by_name.items()):
            if len(kinds) > 1:
                self.log_error(message=f"Segment name '{name}' is used by {len(kinds)} segments ({sorted(kinds)})")

    def _validate_rule_endpoints(self, segments: list[dict[str, Any]], rules: list[dict[str, Any]]) -> None:
        """A rule endpoint segment with no prefix and no selector renders as `any` (warning), unless it is the internet."""
        by_id = {segment["id"]: segment for segment in segments if segment.get("id")}
        for rule in rules:
            if rule.get("disabled") or not (rule.get("policy") or {}).get("enabled", True):
                continue
            for side in ("source", "destination"):
                endpoint = rule.get(f"{side}_segment") or {}
                if not endpoint.get("id") or rule.get(f"{side}_prefixes") or rule.get(f"{side}_ip_addresses"):
                    continue
                segment = by_id.get(endpoint["id"], endpoint)
                zone = (segment.get("security_zone") or {}).get("name")
                if zone == INTERNET_ZONE or segment_prefix(segment):
                    continue
                self.log_info(
                    message=(
                        f"WARNING: rule '{rule.get('name', rule.get('id'))}': {side} segment "
                        f"'{segment.get('name', endpoint['id'])}' resolves to no prefix and is not in "
                        f"{INTERNET_ZONE} — it would render as any"
                    )
                )

    def _warn_component_environment(self, components: list[dict[str, Any]]) -> None:
        """An app component in a segment of another environment (log_info: InfrahubCheck has no warning level)."""
        for component in components:
            segment = component.get("network_segment") or {}
            app = component.get("parent") or {}
            app_env, seg_env = app.get("environment"), segment.get("environment")
            if app_env and seg_env and app_env != seg_env:
                self.log_info(
                    message=(
                        f"WARNING: component '{component.get('name')}' of app '{app.get('name')}' "
                        f"(environment '{app_env}') lives in segment '{segment.get('name')}' "
                        f"(environment '{seg_env}')"
                    )
                )

    def _validate_component_tags(self, segments: list[dict[str, Any]], components: list[dict[str, Any]]) -> None:
        """A component of a microsegmented segment without a group tag has no group to be contracted by."""
        modes = {segment["id"]: segment.get("isolation_mode") for segment in segments if segment.get("id")}
        for component in components:
            segment = component.get("network_segment") or {}
            if modes.get(segment.get("id")) == "microsegmented" and not component.get("security_tag"):
                self.log_error(
                    message=(
                        f"Component '{component.get('fqdn') or component.get('name')}' in microsegmented segment "
                        f"'{segment.get('name', segment.get('id'))}' has no security_tag"
                    )
                )

    def _validate_tags(self, tags: list[dict[str, Any]], profiles: list[dict[str, Any]]) -> None:
        """A tag groups one component or one segment (a shared tag merges two groups; none is an orphan),
        its group_id is in the pool band, and no profile's reserved group is in that band."""
        pool, problem = tag_pool(profiles)
        low, high = (pool.get("start_range"), pool.get("end_range")) if pool else (None, None)
        pool_name = (pool or {}).get("name")
        if tags and pool is None:
            self.log_error(
                message=f"No group tag pool ({problem}): the band of {len(tags)} group tag(s) cannot be checked"
            )

        def _in_band(value: int) -> bool | None:
            """Whether ``value`` is in the pool band; None when the band is unknown."""
            return None if low is None or high is None else low <= value <= high

        for tag in sorted(tags, key=lambda item: str(item.get("name"))):
            name = tag.get("name", tag.get("id"))
            fqdns = sorted(str(component.get("fqdn")) for component in tag.get("components") or [])
            segments = sorted(str(segment.get("name")) for segment in tag.get("network_segments") or [])
            if fqdns and segments:
                self.log_error(message=f"Security tag '{name}' groups both components {fqdns} and segments {segments}")
            elif not fqdns and not segments:
                self.log_info(message=f"WARNING: security tag '{name}' groups no component and no segment (orphan)")
            for kind, holders in (("components", fqdns), ("segments", segments)):
                if len(holders) > 1:
                    self.log_error(message=f"Security tag '{name}' is shared by {len(holders)} {kind}: {holders}")
            group_id = tag.get("group_id")
            if _in_band(group_id or 0) is False:
                self.log_error(message=f"Security tag '{name}' group_id {group_id} is outside {pool_name} {low}-{high}")
        for profile in sorted(profiles, key=lambda item: str(item.get("name"))):
            for reserved in RESERVED_GROUPS:
                value = profile.get(f"{reserved}_group_id")
                if value is not None and _in_band(value):
                    self.log_error(
                        message=(
                            f"Group-policy profile '{profile.get('name')}' {reserved} group {value} is inside "
                            f"{pool_name} {low}-{high}: a drawn tag could mean it"
                        )
                    )

    def _validate_rule_tags(
        self, rules: list[dict[str, Any]], tags: list[dict[str, Any]], segments: list[dict[str, Any]]
    ) -> None:
        """A rule side carries a group tag only where its segment is microsegmented, and the tag
        is of a component of that side's segment (an intra-segment rule, or a component
        dependency into or out of a microsegmented segment)."""
        tag_segments = {
            tag["id"]: {(component.get("network_segment") or {}).get("id") for component in tag.get("components") or []}
            for tag in tags
            if tag.get("id")
        }
        modes = {segment["id"]: segment.get("isolation_mode") for segment in segments if segment.get("id")}
        for rule in rules:
            rule_name = rule.get("name", rule.get("id"))
            for side in ("source", "destination"):
                tag = rule.get(f"{side}_security_tag")
                if not tag:
                    continue
                segment = rule.get(f"{side}_segment") or {}
                segment_name = segment.get("name", segment.get("id"))
                if modes.get(segment.get("id")) != "microsegmented":
                    self.log_error(
                        message=(
                            f"Rule '{rule_name}' carries a {side} group tag but its {side} segment "
                            f"'{segment_name}' is not microsegmented"
                        )
                    )
                elif segment.get("id") not in tag_segments.get(tag.get("id"), set()):
                    self.log_error(
                        message=(
                            f"Rule '{rule_name}': {side} tag '{tag.get('name', tag.get('id'))}' "
                            f"groups no component of its {side} segment '{segment_name}'"
                        )
                    )

    def _validate_contracts(self, segments: list[dict[str, Any]], tags: list[dict[str, Any]]) -> None:
        """Every rule between two groups is a row of the contract builder, with that rule's name;
        a rule into a microsegmented segment without its component's tag opens every group of it (warning)."""
        by_segment, by_tag = segment_groups(segments, tags)
        rows = {(row["src"], row["dst"]): set(row["rules"]) for row in get_vrf_contracts(segments, tags)}
        for segment in sorted(segments, key=lambda item: str(item.get("name"))):
            for rule in segment_flows(segment)[0]["rules"]:
                if rule.get("action") != "permit":
                    continue
                name = str(rule.get("name") or rule.get("id"))
                src_id = str((rule.get("source_segment") or {}).get("id") or segment.get("id"))
                dst_id = str((rule.get("destination_segment") or {}).get("id"))
                src = rule_groups(rule, "source", src_id, by_segment, by_tag)
                dst = rule_groups(rule, "destination", dst_id, by_segment, by_tag)
                if len(dst) > 1:
                    self.log_info(
                        message=(
                            f"WARNING: rule '{name}' into microsegmented segment '{dst[0].segment}' names no "
                            f"component: it opens all {len(dst)} groups of the segment"
                        )
                    )
                if not src or not dst or src[0].vrf != dst[0].vrf:
                    continue
                pair = (src[0].name, dst[0].name)
                if pair[0] == pair[1] and src[0].intra_group == "permit":
                    continue
                if name not in rows.get(pair, set()):
                    self.log_error(
                        message=f"Rule '{name}' between groups {pair[0]} -> {pair[1]} has no group contract row"
                    )
