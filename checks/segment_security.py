"""Validate the security invariants every network segment kind must hold."""

from __future__ import annotations

from typing import Any

from infrahub_sdk.checks import InfrahubCheck

from utils.data_cleaning import clean_data
from utils.exchange_transit import (
    exchange_peer_zones,
    namespace_type_for_environment,
    zone_name_for_namespace_type,
)
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
    - an app component's environment should match its segment's (warning).
    """

    query = "segment_security"

    def validate(self, data: Any) -> None:
        cleaned = clean_data(data)
        segments = cleaned.get("ManagedNetworkSegment") or []
        for segment in segments:
            self._validate_zone(segment)
        self._validate_unique_names(segments)
        self._validate_rule_endpoints(segments, cleaned.get("SecurityPolicyRule") or [])
        self._warn_component_environment(cleaned.get("AppComponent") or [])

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
