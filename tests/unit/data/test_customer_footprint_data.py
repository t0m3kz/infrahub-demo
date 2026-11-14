"""Every customer footprint in the demo data and the integration data states a design that holds its use."""

from __future__ import annotations

from collections import Counter, defaultdict
from dataclasses import replace
from pathlib import Path

import pytest

from tests.integration.demo_data import ROOT, Declared, declared, name
from utils.exchange_transit import is_exchange_zone

_DEMOS = ROOT / "data" / "demos"
_BOOTSTRAP = ROOT / "data" / "bootstrap"
_KINDS = {
    "TopologyCustomerDC": "TopologyCustomerTemplateDC",
    "TopologyCustomerColocation": "TopologyCustomerTemplateColo",
    "TopologyCustomerCloud": "TopologyCustomerTemplateCloud",
}
_DATASETS = sorted(
    [path for path in _DEMOS.iterdir() if path.is_dir()] + [ROOT / "tests" / "integration" / "data"],
    key=lambda path: path.name,
)


def _templates(template_kind: str, dataset: Path) -> dict[str, dict]:
    """Template name -> its row: the bootstrap tiers and the custom ones of ``dataset``."""
    rows = {str(r.item["name"]): r.item for r in declared(template_kind, _BOOTSTRAP)}
    rows.update({str(r.item["name"]): r.item for r in declared(template_kind, dataset)})
    return rows


def _footprints(kind: str, dataset: Path) -> dict[str, list[Declared]]:
    """Footprint name -> every declaration of it in ``dataset`` (an upsert elsewhere repeats it)."""
    found: dict[str, list[Declared]] = defaultdict(list)
    for footprint in declared(kind, dataset):
        found[name(footprint)].append(footprint)
    return found


def _vxlan_segments(dataset: Path) -> Counter[str]:
    """Footprint name -> the VXLAN segments naming it, exchange-zone segments (the operator's) left out."""
    counted: Counter[str] = Counter()
    for segment in declared("ManagedVxlanSegment", dataset):
        if is_exchange_zone(segment.item.get("security_zone")):
            continue
        for footprint in segment.item.get("customer_deployments") or []:
            counted[str(footprint)] += 1
    return counted


def _cloud_use(dataset: Path) -> tuple[Counter[str], Counter[tuple[str, str]]]:
    """(cloud segments by footprint name, virtual networks by (account, region))."""
    segments: Counter[str] = Counter(
        str(s.item.get("customer_deployment")) for s in declared("CloudNetworkSegment", dataset)
    )
    networks: Counter[tuple[str, str]] = Counter(
        (str(n.item.get("account")), str(n.item.get("region"))) for n in declared("CloudVirtualNetwork", dataset)
    )
    return segments, networks


@pytest.mark.parametrize("dataset", _DATASETS, ids=lambda path: path.name)
@pytest.mark.parametrize("kind", _KINDS)
def test_every_declaration_of_a_footprint_states_one_design_of_its_own_kind(kind: str, dataset: Path) -> None:
    """No declaration without a design, none naming a template of another kind."""
    templates = _templates(_KINDS[kind], dataset)
    problems: list[str] = []
    for footprint_name, declarations in _footprints(kind, dataset).items():
        designs = {str(d.item.get("design")) for d in declarations}
        if "None" in designs:
            problems.append(f"{footprint_name}: a declaration has no design")
        if len(designs) > 1:
            problems.append(f"{footprint_name}: declarations disagree: {sorted(designs)}")
        problems.extend(
            f"{footprint_name}: design {design!r} is no {_KINDS[kind]}"
            for design in designs - {"None"} - set(templates)
        )
    assert not problems, problems


@pytest.mark.parametrize("dataset", _DATASETS, ids=lambda path: path.name)
@pytest.mark.parametrize("kind", ["TopologyCustomerDC", "TopologyCustomerColocation"])
def test_the_segments_of_a_footprint_fit_its_designs_segment_quota(kind: str, dataset: Path) -> None:
    """The VXLAN segments naming a data center or colocation footprint do not exceed max_segments of its design."""
    templates = _templates(_KINDS[kind], dataset)
    used = _vxlan_segments(dataset)
    over = [
        f"{footprint_name} ({declarations[0].item.get('design')}): {used[footprint_name]} segments, max "
        f"{templates[str(declarations[0].item['design'])]['max_segments']}"
        for footprint_name, declarations in _footprints(kind, dataset).items()
        if str(declarations[0].item.get("design")) in templates
        and used[footprint_name] > templates[str(declarations[0].item["design"])]["max_segments"]
    ]
    assert not over, over


@pytest.mark.parametrize("dataset", _DATASETS, ids=lambda path: path.name)
def test_the_cloud_segments_and_networks_of_a_footprint_fit_its_design(dataset: Path) -> None:
    """A cloud footprint has no more cloud segments than max_segments and its account no more virtual networks in the
    footprint's region than max_virtual_networks.
    """
    templates = _templates(_KINDS["TopologyCustomerCloud"], dataset)
    segments, networks = _cloud_use(dataset)
    over: list[str] = []
    for footprint_name, declarations in _footprints("TopologyCustomerCloud", dataset).items():
        item = declarations[0].item
        design = templates.get(str(item.get("design")))
        if design is None:
            continue
        if segments[footprint_name] > design["max_segments"]:
            over.append(f"{footprint_name}: {segments[footprint_name]} segments, max {design['max_segments']}")
        used = networks[(str(item["account"]), str(item["parent"]))]
        if used > design["max_virtual_networks"]:
            over.append(f"{footprint_name}: {used} virtual networks, max {design['max_virtual_networks']}")
    assert not over, over


def test_the_scan_sees_footprints_of_every_kind() -> None:
    """Guard: the walker finds footprints of the three kinds, so the tests above are not vacuous."""
    for kind in _KINDS:
        assert any(_footprints(kind, dataset) for dataset in _DATASETS), kind
    assert any(_vxlan_segments(dataset) for dataset in _DATASETS)
    assert any(_cloud_use(dataset)[1] for dataset in _DATASETS)


def test_a_missing_or_foreign_design_fails_the_scan(monkeypatch: pytest.MonkeyPatch) -> None:
    """The negative cases: a declaration with no design, and one naming another kind's (custom) template."""
    dataset = _DEMOS / "30_all"
    kind = "TopologyCustomerColocation"
    original = declared
    colocation = next(iter(_footprints(kind, dataset).values()))[0]

    def with_item(item: dict) -> None:
        replaced = replace(colocation, item=item)
        monkeypatch.setitem(globals(), "declared", lambda k, root: [replaced] if k == kind else original(k, root))

    with_item({k: v for k, v in colocation.item.items() if k != "design"})
    with pytest.raises(AssertionError, match="has no design"):
        test_every_declaration_of_a_footprint_states_one_design_of_its_own_kind(kind, dataset)

    with_item({**colocation.item, "design": "M_INTERNAL"})
    with pytest.raises(AssertionError, match="is no TopologyCustomerTemplateColo"):
        test_every_declaration_of_a_footprint_states_one_design_of_its_own_kind(kind, _DEMOS / "30_all")
