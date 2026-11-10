"""The internet is a zone, not a segment: SecurityPolicyRule.destination_zone replaces ManagedExternalSegment."""

from __future__ import annotations

from tests.integration.demo_data import BOOTSTRAP, declared, name, relationship, schema
from utils.exchange_transit import INTERNET_ZONE


def test_a_rule_has_an_optional_destination_zone_peering_a_security_zone() -> None:
    """One optional peer of SecurityZone, next to the other destination alternatives."""
    zone = relationship("SecurityPolicyRule", "destination_zone")

    assert zone is not None
    assert (zone["peer"], zone["cardinality"], zone["optional"]) == ("SecurityZone", "one", True)
    assert zone["identifier"] == "policyrule_destination_zone"


def test_the_order_weights_of_a_rule_stay_unique() -> None:
    """The UI order is the merge of the fields, and a tie is numbered by the server."""
    weights = [r["order_weight"] for r in schema()["SecurityPolicyRule"].relationships.values()]

    assert len(weights) == len(set(weights))


def test_the_internet_zone_the_code_names_is_a_bootstrap_zone() -> None:
    """INTERNET_ZONE (utils/exchange_transit.py) is the zone data/bootstrap/24_security_zones.yml declares."""
    assert INTERNET_ZONE in {name(zone) for zone in declared("SecurityZone", BOOTSTRAP)}
