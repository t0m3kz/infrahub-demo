"""Corpus test: every rendered config declares exactly ONE BGP ASN.

A router runs one BGP ASN per routing instance. Under ``ebgp-ibgp`` the underlay
is eBGP on a per-device ASN and the overlay is iBGP on the fabric-wide ASN, so two
BGP configs survive the merge and a naive template emits two ``router bgp``
stanzas with DIFFERENT ASNs. The device rejects the second — or, worse, accepts
the first and ignores the rest, and half the routing disappears with no error.

``transforms/helpers/bgp.py``'s ``_collapse_to_single_instance`` prevents that by
anchoring on the overlay ASN and re-asserting the underlay ASN per session
(``local-as … no-prepend replace-as``). ``tests/unit/test_bgp_single_instance.py``
tests that function directly; this test asserts the property actually holds in the
rendered output, for every device role × platform × routing strategy the fixture
corpus covers. A template that reopens ``router bgp`` to add address-family
config is fine — same ASN, config-mode re-entry — so the assertion is on the
number of DISTINCT ASNs, not on the number of stanzas.

Per-neighbor ``local-as`` overrides are deliberately excluded: they are how the
single instance presents the underlay ASN to an eBGP peer, so counting them would
flag the fix as the bug.
"""

from __future__ import annotations

import json
import re
from pathlib import Path

import pytest

# EOS / NX-OS / SONiC-FRR / IOS. Anchored so a per-neighbor line can never match.
_CLI_INSTANCE_ASN = re.compile(r"^\s*router bgp (\d+)\s*$", re.MULTILINE)

# Nokia SR OS MD-CLI. The instance ASN is `bgp <instance> local-as <asn>`; the
# per-neighbor override is `... neighbor "<ip>" local-as as-number <asn>`, which
# this cannot match — it requires `local-as` immediately followed by the number.
_SROS_INSTANCE_ASN = re.compile(r"^.*configure router .* bgp \d+ local-as (\d+)\s*$", re.MULTILINE)


def _asns_in_config(text: str) -> set[int]:
    """Every distinct BGP instance ASN declared in one rendered config."""
    stripped = text.lstrip()
    if stripped.startswith("{"):
        # SONiC ConfigDB is JSON, not CLI. BGP_GLOBALS holds the instance ASN per
        # VRF; per-neighbor overrides live under BGP_NEIGHBOR and are not read here.
        try:
            config = json.loads(text)
        except json.JSONDecodeError:  # pragma: no cover - a corrupt fixture
            return set()
        globals_table = config.get("BGP_GLOBALS") or {}
        return {int(entry["local_asn"]) for entry in globals_table.values() if entry.get("local_asn") is not None}

    return {int(m) for m in _CLI_INSTANCE_ASN.findall(text)} | {int(m) for m in _SROS_INSTANCE_ASN.findall(text)}


def _config_fixtures(root_dir: Path) -> list[Path]:
    return sorted((root_dir / "tests" / "smoke" / "configs").glob("*/output.txt"))


@pytest.fixture(scope="module")
def routed_fixtures(root_dir: Path) -> list[Path]:
    """Fixtures that declare a BGP instance at all.

    Firewalls, proxies and load-balancers render no ``router bgp``; they are not
    exempted by name, just filtered out by having no ASN to disagree about.
    """
    fixtures = _config_fixtures(root_dir)
    assert fixtures, "no config fixtures found — run tests/smoke/generate_config_fixtures.py"
    return [path for path in fixtures if _asns_in_config(path.read_text())]


class TestAsnExtraction:
    """The corpus assertions are only as good as the extractor. A regex that
    quietly stopped matching would turn every test below into a no-op, so each
    syntax is checked against a config that MUST be flagged and one that must not.
    """

    @pytest.mark.parametrize(
        ("label", "text"),
        [
            ("eos", "router bgp 65001\n   neighbor 1.1.1.1 remote-as 65100\nrouter bgp 65000\n"),
            (
                "sros",
                'A admin configure router "Base" bgp 1 local-as 65000\n'
                'A admin configure router "Base" bgp 2 local-as 65001\n',
            ),
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
                "sros",
                'A admin configure router "Base" bgp 1 local-as 65000\n'
                'A admin configure router "Base" bgp 1 neighbor "1.1.1.1" local-as as-number 65001\n',
            ),
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
    """The direction of the collapse, checked in the rendered output.

    Anchoring on the underlay ASN instead would still produce one instance and
    still pass the test above, while turning every EVPN session into eBGP and
    breaking route reflection. Under ebgp-ibgp the fixtures use overlay 65000 /
    underlay 65001, so the instance must be 65000 and 65001 must appear only as a
    per-neighbor override.
    """
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
