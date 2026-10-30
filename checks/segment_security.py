"""Validate the security invariants every network segment kind must hold."""

from __future__ import annotations

import ipaddress
from typing import Any

from transforms.helpers.bgp_external import registered_by_attachment
from transforms.helpers.group_policy import get_vrf_contracts, rule_groups, segment_groups
from transforms.helpers.policy import rule_enabled, segment_flows
from utils.circuit_underlay import SESSION_CIRCUIT
from utils.data_cleaning import clean_data
from utils.exchange_transit import (
    EXCHANGE_PEERS,
    INTERNET_ZONE,
    ROUTED_SEGMENT_KINDS,
    is_exchange_zone,
    namespace_type_for_environment,
    zone_name_for_namespace_type,
)
from utils.group_policy import MICROSEGMENTED, RESERVED_GROUPS, tag_pool
from utils.segment_addressing import segment_prefix

from .base import WarningCheck

_Network = ipaddress.IPv4Network | ipaddress.IPv6Network
_Space = list[tuple[_Network, str | None]]


class CheckSegmentSecurity(WarningCheck):
    """Validate zone, address and identity of every ManagedNetworkSegment kind.

    Runs on settled data (gateways are allocated asynchronously, so a generator
    cannot refuse a rule to an address-less segment; this check does):

    - every segment has a security_zone;
    - a tenant segment's zone follows its environment (p -> PROD-ZONE, else
      NONPROD-ZONE), an exchange-zone segment is exempt;
    - a routed (VLAN/VXLAN) tenant segment's gateway prefix sits in a
      namespace of its environment's type (p -> prod, else non_prod);
      exchange-zone segments are exempt from both environment rules;
    - computed segment names are unique across kinds;
    - a rule endpoint segment resolves to a prefix (or the rule has an explicit
      selector): otherwise it would render as any (a warning: L2-only segments
      have no gateway by design);
    - a rule has one kind of destination: ``destination_zone`` (the internet,
      INTERNET-ZONE and no other zone) excludes a destination segment, prefix,
      address and group tag;
    - an app component's environment should match its segment's (warning);
    - every component of a microsegmented segment has a group tag; a tag groups
      exactly one component or exactly one segment, never both (an orphan is
      a warning); its group_id is in the band of the tag pool the
      group-policy profiles name, and no reserved group of a profile is;
    - a rule side carries a component tag only where its segment is
      microsegmented: inside it, or on a component dependency into or out of
      it;
    - every ``destination_prefixes`` entry of an enabled rule is reachable: inside an on-prem segment prefix, or
      inside a prefix a customer registered behind an external session (a VPC block of a virtual interface with a
      handoff circuit, an office ``lan_prefixes`` or hosted segment and a tunnel's ``registered_prefixes``, behind
      an SD-WAN gateway; transforms/helpers/bgp_external.py registered_by_attachment), in a namespace of the
      source segment's environment (or an exchange peer of it); otherwise no session accepts it and the rule
      aims at nothing (a ``destination_segment`` or ``destination_zone`` rule is not checked here: the internet
      needs no registered prefix);
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
            self._validate_gateway_namespace(segment)
        self._validate_unique_names(segments)
        self._validate_rule_endpoints(segments, rules)
        self._validate_rule_destinations(rules)
        self._validate_destination_prefixes(cleaned, segments, rules)
        self._warn_component_environment(components)
        self._validate_component_tags(segments, components)
        self._validate_tags(tags, cleaned.get("DcimGroupPolicyProfile") or [])
        self._validate_rule_tags(rules, tags, segments)
        self._validate_contracts(segments, tags)

    def _validate_zone(self, segment: dict[str, Any]) -> None:
        """Zone present; tenant zone == f(environment); an exchange-zone segment is exempt."""
        seg_name = segment.get("name", "<unnamed-segment>")
        kind = segment.get("typename")
        zone = (segment.get("security_zone") or {}).get("name")
        if not zone:
            self.log_error(
                message=(f"{kind} '{seg_name}' has no security_zone — zone-based firewalls match any zone on that side")
            )
            return
        if is_exchange_zone(zone):
            # A shared service segment (e.g. the DC-local MANAGEMENT services) sits in an exchange zone by
            # design; ensure_security_zone keeps that zone too (one predicate, so the two never disagree).
            return
        environment = segment["environment"]
        expected = zone_name_for_namespace_type(namespace_type_for_environment(environment))
        if zone != expected:
            self.log_error(
                message=(
                    f"{kind} '{seg_name}' (environment '{environment}') is in zone '{zone}', expected '{expected}'"
                )
            )

    def _validate_gateway_namespace(self, segment: dict[str, Any]) -> None:
        """A routed tenant segment's gateway prefix is in a namespace of its environment's type.

        The zone rule above reads the environment; this one reads where the
        address actually lives, so a production segment addressed in NON-PROD
        (or in a namespace with no type, such as ``default``) cannot pass as
        PROD-ZONE. An L2-only segment (no gateway) has nothing to check.
        """
        if segment.get("typename") not in ROUTED_SEGMENT_KINDS:
            return
        if is_exchange_zone((segment.get("security_zone") or {}).get("name")):
            return
        prefix = (segment.get("gateway") or {}).get("ip_prefix") or {}
        if not prefix:
            return
        environment = segment["environment"]
        namespace = prefix.get("ip_namespace") or {}
        expected = namespace_type_for_environment(environment)
        if namespace.get("namespace_type") != expected:
            self.log_error(
                message=(
                    f"{segment.get('typename')} '{segment.get('name', '<unnamed-segment>')}' "
                    f"(environment '{environment}') has its gateway prefix {prefix.get('prefix')} in namespace "
                    f"'{namespace.get('name')}' (type '{namespace.get('namespace_type')}'), "
                    f"expected a '{expected}' namespace"
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
                if segment_prefix(segment):
                    continue
                self.log_warning(
                    message=(
                        f"rule '{rule.get('name', rule.get('id'))}': {side} segment "
                        f"'{segment.get('name', endpoint['id'])}' resolves to no prefix — it would render as any"
                    )
                )

    def _validate_rule_destinations(self, rules: list[dict[str, Any]]) -> None:
        """A ``destination_zone`` is the internet and the rule's only destination.

        The leaf ACL and the cloud security group cannot enumerate any other zone (they would render as any),
        and a zone next to a segment, selector or tag would be read by some consumers and ignored by others.
        """
        for rule in rules:
            zone = (rule.get("destination_zone") or {}).get("name")
            if not zone or not rule_enabled(rule):
                continue
            name = rule.get("name", rule.get("id"))
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
            cleaned.get("CloudVirtualInterface"),
        )
        environments = {segment["id"]: segment.get("environment") for segment in segments if segment.get("id")}
        for rule in rules:
            if not rule_enabled(rule):
                continue
            environment = environments.get((rule.get("source_segment") or {}).get("id"))
            types = _reachable_types(environment)
            for destination in rule.get("destination_prefixes") or []:
                cidr = destination.get("prefix")
                network = _network(cidr)
                if network is not None and not any(
                    network.prefixlen >= pool.prefixlen
                    and network.network_address in pool
                    and (types is None or kind in types)
                    for pool, kind in space
                ):
                    self.log_error(
                        message=(
                            f"destination {cidr} of rule {rule.get('name', rule.get('id'))} is not reachable: "
                            "no session accepts it (not on-prem space, not registered behind an external session)"
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


def _network(cidr: str | None) -> _Network | None:
    """The network of a CIDR string, None when it is missing or invalid."""
    try:
        return ipaddress.ip_network(cidr, strict=False) if cidr else None
    except ValueError:
        return None


def _reachable_types(environment: str | None) -> frozenset[str] | None:
    """The namespace types a rule from a segment of ``environment`` may reach: its own and its exchange peers.

    None when the rule names no source segment (any namespace type): a PROD segment never reaches a prefix
    registered only in NON-PROD, and the other way round.
    """
    if not environment:
        return None
    own = namespace_type_for_environment(environment)
    return frozenset({own, *EXCHANGE_PEERS[own]})


def _reachable_space(
    segments: list[dict[str, Any]],
    offices: list[dict[str, Any]] | None,
    tunnels: list[dict[str, Any]] | None,
    vifs: list[dict[str, Any]] | None,
) -> _Space:
    """Every (network, namespace_type) a destination prefix may sit in: on-prem and registered behind a session.

    On-prem: the gateway prefix of every segment. Registered: the prefixes a customer owner
    registered behind an attachment point (registered_by_attachment, the edge's own reading); a prefix with no
    customer owner is never accepted, so it is no destination either (routing_invariants.py rule 7 reports it).
    """
    prefixes = [(segment.get("gateway") or {}).get("ip_prefix") or {} for segment in segments]
    prefixes += [
        prefix
        for registered in registered_by_attachment(offices, tunnels, vifs).values()
        for owner, prefix in registered
        if owner
    ]
    return [
        (network, (prefix.get("ip_namespace") or {}).get("namespace_type"))
        for prefix in prefixes
        if (network := _network(prefix.get("prefix")))
    ]
