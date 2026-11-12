"""Validate the security invariants every network segment kind must hold."""

from __future__ import annotations

import ipaddress
from typing import Any

from transforms.helpers.bgp_cloud import CloudLeg, cloud_legs
from transforms.helpers.bgp_external import registered_by_attachment
from transforms.helpers.group_policy import get_vrf_contracts, rule_groups, segment_groups
from transforms.helpers.policy import is_internet_destination, rule_enabled, segment_flows
from utils.circuit_underlay import SESSION_CIRCUIT
from utils.data_cleaning import clean_data
from utils.exchange_transit import (
    EXCHANGE_PEERS,
    INTERNET_ZONE,
    ROUTED_SEGMENT_KINDS,
    TENANT_NAMESPACE_TYPES,
    is_exchange_zone,
    namespace_type_for_environment,
    zone_name_for_namespace_type,
)
from utils.group_policy import MICROSEGMENTED, RESERVED_GROUPS, tag_pool
from utils.ownership import customer_owner
from utils.segment_addressing import own_prefix

from .base import WarningCheck

_Network = ipaddress.IPv4Network | ipaddress.IPv6Network
# (network, namespace type, owner key); owner is None for on-prem space.
_Space = list[tuple[_Network, str | None, str | None]]


class CheckSegmentSecurity(WarningCheck):
    """Validate zone, address and identity of every ManagedNetworkSegment kind, on settled data."""

    query = "segment_security"

    def validate(self, data: Any) -> None:
        cleaned = clean_data(data)
        segments = cleaned.get("ManagedNetworkSegment") or []
        rules = cleaned.get("SecurityPolicyRule") or []
        components = cleaned.get("AppComponent") or []
        tags = cleaned.get("SecurityTag") or []
        for segment in segments:
            self._validate_zone(segment)
            self._validate_gateway_zone(segment)
        self._validate_unique_names(segments)
        self._validate_rule_endpoints(segments, rules)
        self._validate_policy_rules(rules)
        self._validate_environment_separation(rules)
        self._validate_selector_zones(rules)
        self._validate_rule_destinations(rules)
        self._validate_destination_prefixes(cleaned, segments, rules)
        self._warn_component_environment(components)
        self._validate_component_tags(segments, components)
        self._validate_tags(tags, cleaned.get("DcimGroupPolicyProfile") or [])
        self._validate_rule_tags(rules, tags, segments)
        self._validate_contracts(segments, tags)

    def _validate_zone(self, segment: dict[str, Any]) -> None:
        """Tenant zone follows the environment; an exchange-zone segment is exempt."""
        seg_name = segment.get("name", "<unnamed-segment>")
        kind = segment.get("typename")
        zone = segment["security_zone"]["name"]
        if is_exchange_zone(zone):
            # A shared service segment sits in an exchange zone by design.
            return
        environment = segment["environment"]
        expected = zone_name_for_namespace_type(namespace_type_for_environment(environment))
        if zone != expected:
            self.log_error(
                message=(
                    f"{kind} '{seg_name}' (environment '{environment}') is in zone '{zone}', expected '{expected}'"
                )
            )

    def _validate_gateway_zone(self, segment: dict[str, Any]) -> None:
        """A routed tenant segment's owner zone has the VRF type of its environment."""
        if segment.get("typename") not in ROUTED_SEGMENT_KINDS:
            return
        zone = segment["security_zone"]
        if is_exchange_zone(zone["name"]):
            return
        prefix = (segment.get("gateway") or {}).get("ip_prefix") or {}
        if not prefix:
            return
        environment = segment["environment"]
        expected = namespace_type_for_environment(environment)
        if zone.get("vrf_type") != expected:
            self.log_error(
                message=(
                    f"{segment.get('typename')} '{segment.get('name', '<unnamed-segment>')}' "
                    f"(environment '{environment}') has its gateway prefix {prefix.get('prefix')} in security zone "
                    f"'{zone.get('name')}' (type '{zone.get('vrf_type')}'), "
                    f"expected a '{expected}' VRF type"
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
        """A rule endpoint segment with no prefix and no selector renders as `any` (warning)."""
        by_id = {segment["id"]: segment for segment in segments if segment.get("id")}
        for rule in rules:
            if not rule_enabled(rule):
                continue
            for side in ("source", "destination"):
                endpoint = rule.get(f"{side}_segment") or {}
                if not endpoint.get("id") or rule.get(f"{side}_prefixes") or rule.get(f"{side}_ip_addresses"):
                    continue
                segment = by_id.get(endpoint["id"], endpoint)
                if own_prefix(segment):
                    continue
                self.log_warning(
                    message=(
                        f"rule '{rule.get('name', rule.get('id'))}': {side} segment "
                        f"'{segment.get('name', endpoint['id'])}' resolves to no prefix — it would render as any"
                    )
                )

    def _validate_policy_rules(self, rules: list[dict[str, Any]]) -> None:
        """A rule lives in its source segment's policy, has a destination, and is not ``apply_on_switch`` to the
        internet.
        """
        for rule in rules:
            if not rule_enabled(rule):
                continue
            name = rule.get("name", rule.get("id"))
            policy = rule.get("policy") or {}
            policy_segment = (policy.get("segment") or {}).get("id")
            source = rule.get("source_segment") or {}
            if policy_segment and source.get("id") != policy_segment:
                self.log_error(
                    message=(
                        f"Policy '{policy.get('name', policy.get('id'))}' rule '{name}': source segment "
                        f"'{source.get('name') or source.get('id') or '<none>'}' is not the policy's segment — "
                        "a rule belongs in its source segment's policy"
                    )
                )
            if not (
                (rule.get("destination_segment") or {}).get("id")
                or rule.get("destination_zone")
                or rule.get("destination_prefixes")
                or rule.get("destination_ip_addresses")
            ):
                self.log_error(message=f"rule '{name}' has no destination (segment, zone, IP, or prefix)")
            if rule.get("apply_on_switch") and is_internet_destination(rule):
                self.log_error(
                    message=(
                        f"rule '{name}': apply_on_switch to the internet zone — the leaf ACL can only approximate it "
                        "by denying the internal ranges first, and it would force the whole ACL of a firewalled "
                        "segment onto the switch"
                    )
                )

    def _validate_environment_separation(self, rules: list[dict[str, Any]]) -> None:
        """An enabled rule between a PROD and a NON-PROD segment is an error (judged by the zone's ``vrf_type``)."""
        for rule in rules:
            if not rule_enabled(rule):
                continue
            source = _tenant_zone(rule.get("source_segment"))
            destination = _tenant_zone(rule.get("destination_segment"))
            if source and destination and source[1] != destination[1]:
                self.log_error(
                    message=(
                        f"rule '{rule.get('name', rule.get('id'))}': source segment '{source[0]}' is in the "
                        f"{source[1]} namespace and destination segment '{destination[0]}' in the {destination[1]} one; "
                        "prod and non-prod never connect"
                    )
                )

    def _validate_selector_zones(self, rules: list[dict[str, Any]]) -> None:
        """A rule side with a prefix or address selector and no segment names its zone (the render needs it)."""
        for rule in rules:
            if not rule_enabled(rule):
                continue
            for side in ("source", "destination"):
                if not (rule.get(f"{side}_prefixes") or rule.get(f"{side}_ip_addresses")):
                    continue
                if (rule.get(f"{side}_segment") or {}).get("id") or (rule.get(f"{side}_zone") or {}).get("name"):
                    continue
                self.log_error(
                    message=(
                        f"rule '{rule.get('name', rule.get('id'))}': its {side} selector has no {side}_segment and no "
                        f"{side}_zone — name the zone the {side} prefixes or addresses are routed in"
                    )
                )

    def _validate_rule_destinations(self, rules: list[dict[str, Any]]) -> None:
        """A bare ``destination_zone`` is the internet and the rule's only destination."""
        for rule in rules:
            zone = (rule.get("destination_zone") or {}).get("name")
            if not zone or not rule_enabled(rule):
                continue
            name = rule.get("name", rule.get("id"))
            if rule.get("destination_prefixes") or rule.get("destination_ip_addresses"):
                # An explicit zone qualifies a selector rather than denoting a bare destination.
                from transforms.helpers.policy import rule_zone  # noqa: PLC0415
                from utils.routing_domain import RoutingDomainError  # noqa: PLC0415

                try:
                    rule_zone(rule, "destination")
                except RoutingDomainError as exc:
                    self.log_error(message=str(exc))
                continue
            if zone != INTERNET_ZONE:
                self.log_error(
                    message=(
                        f"rule '{name}': destination_zone '{zone}' is not {INTERNET_ZONE} — only the internet has no "
                        "address space to name, any other zone is made of segments: name the segment"
                    )
                )
            others = [
                field
                for field in (
                    "destination_segment",
                    "destination_security_tag",
                    "destination_prefixes",
                    "destination_ip_addresses",
                )
                if rule.get(field)
            ]
            if others:
                self.log_error(
                    message=f"rule '{name}': destination_zone '{zone}' excludes every other destination, found {others}"
                )

    def _validate_destination_prefixes(
        self, cleaned: dict[str, Any], segments: list[dict[str, Any]], rules: list[dict[str, Any]]
    ) -> None:
        """A destination prefix of an enabled rule is on-prem space or registered behind an external session."""
        space = _reachable_space(
            segments,
            cleaned.get("TopologyCustomerOffice"),
            cleaned.get(SESSION_CIRCUIT),
            cloud_legs(cleaned.get("CloudPeeringInterface"), cleaned.get("CloudGatewayAttachment")),
        )
        environments = {segment["id"]: segment.get("environment") for segment in segments if segment.get("id")}
        owners = {segment["id"]: customer_owner(segment.get("owner")) for segment in segments if segment.get("id")}
        for rule in rules:
            if not rule_enabled(rule):
                continue
            source_id = (rule.get("source_segment") or {}).get("id")
            types = _reachable_types(environments.get(source_id))
            source_owner = owners.get(source_id)
            for destination in rule.get("destination_prefixes") or []:
                cidr = destination.get("prefix")
                network = _network(cidr)
                if network is None:
                    continue
                inside = [
                    (kind, owner)
                    for pool, kind, owner in space
                    if network.prefixlen >= pool.prefixlen
                    and network.network_address in pool
                    and (types is None or kind in types)
                ]
                if not inside:
                    self.log_error(
                        message=(
                            f"destination {cidr} of rule {rule.get('name', rule.get('id'))} is not reachable: "
                            "no session accepts it (not on-prem space, not registered behind an external session)"
                        )
                    )
                elif source_owner and all(owner and owner != source_owner for _, owner in inside):
                    self.log_error(
                        message=(
                            f"destination {cidr} of rule {rule.get('name', rule.get('id'))} is registered by "
                            f"{', '.join(sorted({str(owner) for _, owner in inside}))}, not by the owner of its source "
                            f"segment ({source_owner}): the edge never announces {source_owner}'s prefixes to that "
                            "session, so the destination has no route back"
                        )
                    )

    def _warn_component_environment(self, components: list[dict[str, Any]]) -> None:
        """An app component in a segment of another environment (a warning, not an error)."""
        for component in components:
            segment = component.get("network_segment") or {}
            app = component.get("parent") or {}
            app_env, seg_env = app.get("environment"), segment.get("environment")
            if app_env and seg_env and app_env != seg_env:
                self.log_warning(
                    message=(
                        f"component '{component.get('name')}' of app '{app.get('name')}' "
                        f"(environment '{app_env}') lives in segment '{segment.get('name')}' "
                        f"(environment '{seg_env}')"
                    )
                )

    def _validate_component_tags(self, segments: list[dict[str, Any]], components: list[dict[str, Any]]) -> None:
        """A component of a microsegmented segment without a group tag has no group to be contracted by."""
        modes = {segment["id"]: segment.get("isolation_mode") for segment in segments if segment.get("id")}
        for component in components:
            segment = component.get("network_segment") or {}
            if modes.get(segment.get("id")) == MICROSEGMENTED and not component.get("security_tag"):
                self.log_error(
                    message=(
                        f"Component '{component.get('fqdn') or component.get('name')}' in microsegmented segment "
                        f"'{segment.get('name', segment.get('id'))}' has no security_tag"
                    )
                )

    def _validate_tags(self, tags: list[dict[str, Any]], profiles: list[dict[str, Any]]) -> None:
        """A tag groups one component or one segment, its group_id is in the pool band, no reserved group is."""
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
                self.log_warning(message=f"security tag '{name}' groups no component and no segment (orphan)")
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
        """A rule side carries a group tag only in a microsegmented segment, and of one of its components."""
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
                if modes.get(segment.get("id")) != MICROSEGMENTED:
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
        """Every rule between two groups is a contract row; one into a microsegmented segment without a tag warns."""
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
                    self.log_warning(
                        message=(
                            f"rule '{name}' into microsegmented segment '{dst[0].segment}' names no "
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


def _tenant_zone(segment: dict[str, Any] | None) -> tuple[str, str] | None:
    """(segment name, namespace type) of a rule end in a tenant zone (PROD or NON-PROD), else None."""
    segment = segment or {}
    namespace_type = (segment.get("security_zone") or {}).get("vrf_type")
    if not segment.get("id") or namespace_type not in TENANT_NAMESPACE_TYPES:
        return None
    return str(segment.get("name") or segment["id"]), namespace_type


def _network(cidr: str | None) -> _Network | None:
    """The network of a CIDR string, None when it is missing or invalid."""
    try:
        return ipaddress.ip_network(cidr, strict=False) if cidr else None
    except ValueError:
        return None


def _reachable_types(environment: str | None) -> frozenset[str] | None:
    """Namespace types a rule from ``environment`` may reach (its own and exchange peers); None: any."""
    if not environment:
        return None
    own = namespace_type_for_environment(environment)
    return frozenset({own, *EXCHANGE_PEERS[own]})


def _reachable_space(
    segments: list[dict[str, Any]],
    offices: list[dict[str, Any]] | None,
    tunnels: list[dict[str, Any]] | None,
    legs: list[CloudLeg],
) -> _Space:
    """Every (network, namespace_type, owner) a destination may sit in: on-prem, or registered by a customer behind a
    session.
    """
    space: _Space = []
    for segment in segments:
        prefix = (segment.get("gateway") or {}).get("ip_prefix") or {}
        if network := _network(prefix.get("prefix")):
            space.append((network, (segment.get("security_zone") or {}).get("vrf_type"), None))
    for registered in registered_by_attachment(offices, tunnels, legs).values():
        for registration in registered:
            if registration.customer and (network := _network(registration.prefix.get("prefix"))):
                space.append((network, registration.zone.get("vrf_type"), registration.customer))
    return space
