"""Corpus test: every rendered config declares exactly ONE BGP ASN."""

from __future__ import annotations

import json
import re
from pathlib import Path

import pytest

# EOS / NX-OS / SONiC-FRR / IOS. Anchored so a per-neighbor line can never match.
_CLI_INSTANCE_ASN = re.compile(r"^\s*router bgp (\d+)\s*$", re.MULTILINE)


def _asns_in_config(text: str) -> set[int]:
    """Every distinct BGP instance ASN declared in one rendered config."""
    stripped = text.lstrip()
    if stripped.startswith("{"):
        # SONiC ConfigDB is JSON: BGP_GLOBALS holds the instance ASN per VRF; neighbor overrides are not read.
        try:
            config = json.loads(text)
        except json.JSONDecodeError:  # pragma: no cover - a corrupt fixture
            return set()
        globals_table = config.get("BGP_GLOBALS") or {}
        return {int(entry["local_asn"]) for entry in globals_table.values() if entry.get("local_asn") is not None}

    return {int(m) for m in _CLI_INSTANCE_ASN.findall(text)}


def _config_fixtures(root_dir: Path) -> list[Path]:
    return sorted((root_dir / "tests" / "smoke" / "configs").glob("*/output.txt"))


@pytest.fixture(scope="module")
def routed_fixtures(root_dir: Path) -> list[Path]:
    """Fixtures that declare a BGP instance at all."""
    fixtures = _config_fixtures(root_dir)
    assert fixtures, "no config fixtures found — run tests/smoke/generate_config_fixtures.py"
    return [path for path in fixtures if _asns_in_config(path.read_text())]


class TestAsnExtraction:
    """The corpus assertions are only as good as the extractor."""

    @pytest.mark.parametrize(
        ("label", "text"),
        [
            ("eos", "router bgp 65001\n   neighbor 1.1.1.1 remote-as 65100\nrouter bgp 65000\n"),
            ("configdb", '{"BGP_GLOBALS": {"default": {"local_asn": 65000}, "Vrf1": {"local_asn": 65001}}}'),
        ],
    )
    def test_two_instances_are_detected(self, label: str, text: str) -> None:
        assert _asns_in_config(text) == {65000, 65001}, label

    @pytest.mark.parametrize(
        ("label", "text"),
        [
            ("eos", "router bgp 65000\n   neighbor 1.1.1.1 local-as 65001 no-prepend replace-as\n"),
            (
                "configdb",
                '{"BGP_GLOBALS": {"default": {"local_asn": 65000}}, "BGP_NEIGHBOR": {"x": {"local_asn": "65001"}}}',
            ),
        ],
    )
    def test_a_per_neighbor_override_is_not_a_second_instance(self, label: str, text: str) -> None:
        """This is the fix, not the bug — counting it would invert the test."""
        assert _asns_in_config(text) == {65000}, label

    def test_reopening_the_same_instance_is_not_a_violation(self) -> None:
        """Arista templates re-enter `router bgp <asn>` to add address-family
        config. Same ASN, config-mode re-entry, two stanzas, one instance."""
        text = "router bgp 65000\n   neighbor X remote-as 65000\nrouter bgp 65000\n   vlan 100\n"
        assert _asns_in_config(text) == {65000}


def test_the_corpus_actually_covers_bgp(routed_fixtures: list[Path]) -> None:
    """Guard the guard: if the ASN extraction silently stopped matching, every
    assertion below would pass on an empty set."""
    assert len(routed_fixtures) > 50, f"only {len(routed_fixtures)} fixtures yielded a BGP ASN — extraction is broken"


def test_every_config_declares_exactly_one_bgp_asn(routed_fixtures: list[Path]) -> None:
    offenders: list[str] = []
    for path in routed_fixtures:
        asns = _asns_in_config(path.read_text())
        if len(asns) > 1:
            offenders.append(f"{path.parent.name}: {sorted(asns)}")

    assert not offenders, "configs declaring more than one BGP ASN:\n  " + "\n  ".join(offenders)


def test_ebgp_ibgp_configs_anchor_on_the_overlay_asn(root_dir: Path) -> None:
    """The direction of the collapse, checked in the rendered output."""
    overlay_asn, underlay_asn = 65000, 65001
    checked = 0

    for path in _config_fixtures(root_dir):
        if not path.parent.name.endswith("_ebgp_ibgp"):
            continue
        asns = _asns_in_config(path.read_text())
        if not asns:
            continue
        assert asns == {overlay_asn}, (
            f"{path.parent.name}: instance ASN is {sorted(asns)}, expected only the overlay ASN "
            f"{overlay_asn} — anchoring on the underlay ASN {underlay_asn} makes EVPN sessions eBGP"
        )
        checked += 1

    assert checked > 10, f"only {checked} ebgp_ibgp fixtures checked — the scenario may have been renamed"
