"""The internet is a zone, not a segment: SecurityPolicyRule.destination_zone replaces ManagedExternalSegment."""

from __future__ import annotations

from pathlib import Path

from tests.integration.demo_data import BOOTSTRAP, declared, name, relationship, schema
from utils.exchange_transit import INTERNET_ZONE

ROOT = Path(__file__).resolve().parents[2]


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


def test_there_is_no_external_segment_kind_left() -> None:
    """Neither the node nor its bootstrap data survives: the internet owns no segment."""
    assert "ManagedExternalSegment" not in schema()
    assert not declared("ManagedExternalSegment", BOOTSTRAP)
    assert not (ROOT / "data" / "bootstrap" / "27_external_segments.yml").exists()


def test_the_internet_zone_the_code_names_is_a_bootstrap_zone() -> None:
    """INTERNET_ZONE (utils/exchange_transit.py) is the zone data/bootstrap/24_security_zones.yml declares."""
    assert INTERNET_ZONE in {name(zone) for zone in declared("SecurityZone", BOOTSTRAP)}


def test_no_object_file_points_a_rule_at_a_segment_called_internet() -> None:
    """The Zscaler and the C006 internet rules name the zone."""
    offenders = [
        str(path.relative_to(ROOT))
        for path in (ROOT / "data").rglob("*.yml")
        if 'destination_segment: "internet"' in path.read_text()
    ]

    assert offenders == []
