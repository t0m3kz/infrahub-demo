"""Every routed tenant segment and the customer that owns it has a unique routing tag."""

from __future__ import annotations

from collections import defaultdict
from typing import Any

from infrahub_sdk.checks import InfrahubCheck

from utils.data_cleaning import clean_data
from utils.exchange_transit import is_routed_tenant_segment
from utils.routing_identity import ROUTING_TAG_MAX, ROUTING_TAG_MIN, customer_community, segment_community

CUSTOMER_KIND = "OrganizationCustomer"
VLAN_SEGMENT_KIND = "ManagedVlanSegment"


class CheckRoutingTags(InfrahubCheck):
    """The routing tags behind the customer and segment communities are complete, in range and unique (ERRORs).

    A community is a tag for reading the routing table, never a filter (utils/routing_identity.py), but an
    untagged segment renders no community and a tag given twice reads as the wrong customer or segment:

    - a routed VXLAN tenant segment (utils/exchange_transit.is_routed_tenant_segment) has a tag;
      a VLAN segment is tagged by the application generator only, so one no application uses is reported untagged
      (an info line), not an error;
    - the customer owning such a segment has a tag;
    - a tag is within 1..65534, the 16-bit community value (65535 is reserved);
    - a tag is on one customer at most, and on one segment at most (VLAN and VXLAN segments share one pool).

    The community each tagged object renders is reported as an info line.
    """

    query = "routing_tags"

    def validate(self, data: Any) -> None:
        cleaned = clean_data(data)
        customers = cleaned.get(CUSTOMER_KIND) or []
        segments = cleaned.get("ManagedNetworkSegment") or []
        owned = self._validate_segments(segments)
        self._validate_customers(customers, owned)

    def _validate_segments(self, segments: list[dict[str, Any]]) -> dict[str, list[str]]:
        """Check every segment; return owner id -> names of the live routed tenant segments it owns."""
        owned: dict[str, list[str]] = defaultdict(list)
        by_tag: dict[int, list[str]] = defaultdict(list)
        for segment in sorted(segments, key=lambda item: str(item.get("name"))):
            label = f"{segment.get('typename')} '{segment.get('name')}'"
            tag = segment.get("routing_tag")
            live_routed = is_routed_tenant_segment(segment)
            if live_routed:
                owned[str((segment.get("owner") or {}).get("id"))].append(str(segment.get("name")))
            if tag is None:
                if live_routed and segment.get("typename") == VLAN_SEGMENT_KIND:
                    # Only the application generator tags a VLAN segment, so one no application uses is untagged by
                    # design: reported, not an error (the check cannot tell it from a missed run without the usage).
                    self.log_info(message=f"{label}: routed tenant segment is untagged (no application uses it)")
                elif live_routed:
                    self.log_error(
                        message=f"{label}: routed tenant segment has no routing_tag (rule: every routed segment is tagged)"
                    )
                continue
            by_tag[tag].append(label)
            if self._in_range(label, tag):
                self.log_info(message=f"{label} renders {segment_community(tag)}")
        self._report_duplicates("segment", by_tag)
        return owned

    def _validate_customers(self, customers: list[dict[str, Any]], owned: dict[str, list[str]]) -> None:
        by_tag: dict[int, list[str]] = defaultdict(list)
        for customer in sorted(customers, key=lambda item: str(item.get("org_id"))):
            label = f"{CUSTOMER_KIND} '{customer.get('org_id')}'"
            tag = customer.get("routing_tag")
            if tag is None:
                if segments := owned.get(str(customer.get("id"))):
                    self.log_error(
                        message=f"{label}: owns routed segment(s) {', '.join(segments)} but has no routing_tag "
                        "(rule: every customer owning a routed segment is tagged)"
                    )
                continue
            by_tag[tag].append(label)
            if self._in_range(label, tag):
                self.log_info(message=f"{label} renders {customer_community(tag)}")
        self._report_duplicates("customer", by_tag)

    def _in_range(self, label: str, tag: int) -> bool:
        """True when ``tag`` is a valid community value; otherwise log the error and return False."""
        if ROUTING_TAG_MIN <= tag <= ROUTING_TAG_MAX:
            return True
        self.log_error(
            message=f"{label}: routing_tag {tag} is outside {ROUTING_TAG_MIN}..{ROUTING_TAG_MAX} "
            "(rule: a tag is a 16-bit community value, 65535 reserved)"
        )
        return False

    def _report_duplicates(self, what: str, by_tag: dict[int, list[str]]) -> None:
        for tag, labels in sorted(by_tag.items()):
            if len(labels) > 1:
                self.log_error(
                    message=f"routing_tag {tag} is on {len(labels)} {what}s: {', '.join(labels)} "
                    f"(rule: a {what} routing tag is unique)"
                )
