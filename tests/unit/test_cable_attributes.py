"""The derived DcimCable attributes: printed ID, jacket colour and length from the suite layout.

Pure-function tests of ``generators/helpers/cable_attributes.py``, the pins between its tables and the schema
(``schemas/base/dcim.yml`` is the source of the cable types and colours) and the proof that cable IDs do not
collide over the cables the cabling planner produces.
"""

from __future__ import annotations

import re
from typing import Any

import pytest
import yaml
from conftest import MockInterface

from generators.helpers import CablingPlanner
from generators.helpers.cable_attributes import (
    CABLE_COLOR_BY_TYPE,
    CABLE_ID_HEX_LENGTH,
    CABLE_ID_PREFIX,
    INTRA_RACK_CM,
    LENGTH_ROUNDING_CM,
    SERVICE_LOOP_CM,
    RackPosition,
    cable_attributes,
    cable_color,
    cable_id,
    cable_length_cm,
    endpoint_label,
)
from tests.integration.demo_data import ROOT, attribute, declared

_SLACK = 2 * SERVICE_LOOP_CM
_QSFP28 = "100gbase-x-qsfp28"


def _rack(
    row: int | None = 1,
    index: int | None = 1,
    *,
    suite: str = "suite-1",
    rows: int | None = 4,
    per_row: int | None = 9,
    rack_pitch: int | None = 60,
    row_pitch: int | None = 300,
) -> RackPosition:
    """A rack position in a 4 x 9 suite with the schema's default pitches (60 / 300 cm)."""
    return RackPosition(
        suite_id=suite,
        row_index=row,
        index=index,
        rows=rows,
        racks_per_row=per_row,
        rack_pitch_cm=rack_pitch,
        row_pitch_cm=row_pitch,
    )


class TestCableLength:
    """cable_length_cm: the formula of the module docstring, and None whenever a figure would be a guess."""

    def test_same_rack_is_the_intra_rack_constant(self) -> None:
        """Two ends in one rack: INTRA_RACK_CM, whatever the pitches."""
        assert cable_length_cm(_rack(2, 3), _rack(2, 3)) == INTRA_RACK_CM == 100
        assert cable_length_cm(_rack(2, 3, rack_pitch=None, row_pitch=None), _rack(2, 3)) == INTRA_RACK_CM

    def test_adjacent_racks_in_a_row(self) -> None:
        """One pitch apart: 60 + 2 * 50 = 160, rounded up to 200."""
        assert cable_length_cm(_rack(1, 4), _rack(1, 5)) == 200

    def test_far_racks_in_a_row(self) -> None:
        """Eight pitches apart: 8 * 60 + 100 = 580, rounded up to 600; the order of the ends does not matter."""
        assert cable_length_cm(_rack(1, 1), _rack(1, 9)) == 600
        assert cable_length_cm(_rack(1, 9), _rack(1, 1)) == 600

    def test_adjacent_rows_cross_at_the_nearer_aisle_end(self) -> None:
        """Two end racks one row apart: 300 + (1 + 1) * 60 + 100 = 520, rounded up to 550."""
        assert cable_length_cm(_rack(1, 1), _rack(2, 1)) == 550
        assert cable_length_cm(_rack(1, 9), _rack(2, 9)) == 550

    def test_rows_apart_scale_with_the_row_pitch(self) -> None:
        """Three rows apart from the same end: 900 + 120 + 100 = 1120, rounded up to 1150."""
        assert cable_length_cm(_rack(1, 1), _rack(4, 1)) == 1150

    @pytest.mark.parametrize(
        ("index_a", "index_b", "slots"),
        [
            (1, 1, 2),  # both near the low end
            (9, 9, 2),  # both near the high end
            (5, 5, 10),  # both mid-row: either end is 5 slots away
            (2, 9, 9),  # opposite ends: cross at the one cheaper for the pair (8 + 1), not 2 + 1
            (1, 9, 10),  # the two ends cannot both be short: they share one crossing
        ],
    )
    def test_aisle_end_is_one_shared_crossing(self, index_a: int, index_b: int, slots: int) -> None:
        """The cable crosses the rows at ONE aisle end, the cheaper for the pair: min(left sum, right sum) slots."""
        expected = -(-(300 + slots * 60 + _SLACK) // LENGTH_ROUNDING_CM) * LENGTH_ROUNDING_CM
        assert cable_length_cm(_rack(1, index_a), _rack(2, index_b)) == expected

    @pytest.mark.parametrize(
        ("rack_pitch", "expected"),
        [
            (50, 150),  # 50 + 100 = 150: already a multiple of the rounding step, stays
            (51, 200),  # 151: one cm over, up to the next step
            (49, 150),  # 149: rounds up to 150
            (100, 200),  # 200 exactly
            (101, 250),
        ],
    )
    def test_rounds_up_to_the_next_50_cm(self, rack_pitch: int, expected: int) -> None:
        """Round UP: an exact multiple stays, one centimetre over goes to the next 50."""
        assert cable_length_cm(_rack(1, 1, rack_pitch=rack_pitch), _rack(1, 2, rack_pitch=rack_pitch)) == expected

    def test_the_result_is_an_integer_multiple_of_the_rounding_step(self) -> None:
        """Number attributes are integers; every derived length is a whole number of 50 cm steps."""
        for row_b in range(1, 5):
            for index_b in range(1, 10):
                length = cable_length_cm(_rack(1, 3), _rack(row_b, index_b))
                assert isinstance(length, int) and length % LENGTH_ROUNDING_CM == 0 and length >= INTRA_RACK_CM

    def test_custom_pitches_are_used(self) -> None:
        """The suite's own pitches replace the defaults: 4 * 70 + 100 = 380 -> 400; 2 rows * 400 + 2 * 70 + 100 = 1040 -> 1050."""
        assert cable_length_cm(_rack(1, 1, rack_pitch=70), _rack(1, 5, rack_pitch=70)) == 400
        assert (
            cable_length_cm(_rack(1, 1, rack_pitch=70, row_pitch=400), _rack(3, 1, rack_pitch=70, row_pitch=400))
            == 1050
        )

    def test_different_suites_have_no_length(self) -> None:
        """Two suites share no grid: never guess."""
        assert cable_length_cm(_rack(suite="suite-1"), _rack(suite="suite-2")) is None

    @pytest.mark.parametrize("missing", ["a", "b", "both"])
    def test_an_end_without_a_rack_has_no_length(self, missing: str) -> None:
        """A device with no rack (or a virtual device) is None on that end."""
        end_a = None if missing in ("a", "both") else _rack()
        end_b = None if missing in ("b", "both") else _rack(1, 2)
        assert cable_length_cm(end_a, end_b) is None

    @pytest.mark.parametrize(
        "broken",
        [
            _rack(rack_pitch=None),
            _rack(row_pitch=None),
            _rack(row=None),
            _rack(index=None),
            _rack(rows=None),
            _rack(per_row=None),
            _rack(row=5),  # outside the 4-row grid
            _rack(index=10),  # outside the 9-slot row
            _rack(row=0),
            _rack(index=0),
        ],
    )
    def test_a_missing_or_off_grid_figure_has_no_length(self, broken: RackPosition) -> None:
        """A missing pitch or coordinate, or a rack outside the suite grid, leaves the length unset."""
        # Different row from the healthy rack so both pitches would be needed if the broken one were usable.
        assert cable_length_cm(broken, _rack(2, 5)) is None
        assert cable_length_cm(_rack(2, 5), broken) is None

    def test_a_missing_row_pitch_only_matters_across_rows(self) -> None:
        """The row pitch is not needed inside a row, and the rack pitch not inside one rack."""
        assert cable_length_cm(_rack(1, 1, row_pitch=None), _rack(1, 2, row_pitch=None)) == 200
        assert cable_length_cm(_rack(1, 1, row_pitch=None), _rack(2, 1, row_pitch=None)) is None


class TestCableColor:
    """CABLE_COLOR_BY_TYPE against the schema."""

    @staticmethod
    def _choices(name: str) -> set[str]:
        """The Dropdown choice names DcimCable declares for ``name`` in schemas/base/dcim.yml."""
        schema_attribute = attribute("DcimCable", name)
        assert schema_attribute is not None, f"DcimCable.{name} is not in the schema"
        return {choice["name"] for choice in schema_attribute["choices"]}

    def test_every_cable_type_of_the_schema_has_a_colour(self) -> None:
        """A new `type` choice in schemas/base/dcim.yml fails here until it is given a jacket colour."""
        assert set(CABLE_COLOR_BY_TYPE) == self._choices("type")

    def test_every_colour_used_is_a_color_choice(self) -> None:
        """A colour the table hands out must exist as a `color` choice, or the save would be rejected."""
        assert set(CABLE_COLOR_BY_TYPE.values()) <= self._choices("color")

    def test_the_tia_598_colours(self) -> None:
        """Single-mode yellow, multi-mode aqua, copper blue, DAC black."""
        assert CABLE_COLOR_BY_TYPE == {"smf": "yellow", "mmf": "aqua", "copper": "blue", "dac": "black"}

    @pytest.mark.parametrize("cable_type", [None, "", "coax", "SMF"])
    def test_an_unknown_type_has_no_colour(self, cable_type: str | None) -> None:
        """No entry, no colour: the attribute stays unset."""
        assert cable_color(cable_type) is None

    def test_cable_id_and_length_attributes_are_declared_by_the_schema(self) -> None:
        """The attributes the generators write exist on DcimCable: cable_id (not unique), length_cm a Number.

        cable_id must NOT be ``unique``: Infrahub counts an unset value as a value, so the second cable declared in a
        data file without one fails to load with "Violates uniqueness constraint 'cable_id'" (found on the live stack).
        The id is a hash of the endpoint labels, as unique as the cable's own name.
        """
        assert not (attribute("DcimCable", "cable_id") or {}).get("unique")
        assert (attribute("DcimCable", "length_cm") or {}).get("kind") == "Number"


class TestCableId:
    """cable_id: short, deterministic, from the two ends only."""

    def test_format(self) -> None:
        """CBL- plus 10 upper-case hex digits: short enough to print on a label."""
        assert re.fullmatch(rf"{CABLE_ID_PREFIX}[0-9A-F]{{{CABLE_ID_HEX_LENGTH}}}", cable_id(["a-eth1", "b-eth1"]))

    def test_stable_and_order_independent(self) -> None:
        """The same two ends give the same ID on every run, whichever end is named first."""
        assert cable_id(["leaf-01-Ethernet1/49", "spine-01-Ethernet1/1"]) == cable_id(
            ["spine-01-Ethernet1/1", "leaf-01-Ethernet1/49"]
        )
        assert cable_id(["x-1", "y-1"]) == cable_id(["x-1", "y-1"])

    def test_pinned_value(self) -> None:
        """A change to the derivation (every printed label would change) shows here."""
        assert cable_id(["leaf-01-Ethernet1/49", "spine-01-Ethernet1/1"]) == "CBL-ED8FEE1026"

    def test_different_ends_differ(self) -> None:
        """A different port on one end is a different cable and a different ID."""
        assert cable_id(["a-1", "b-1"]) != cable_id(["a-1", "b-2"])
        assert cable_id(["a-1", "b-1"]) != cable_id(["a-1", "c-1"])

    def test_endpoint_label_is_the_cable_name_part(self) -> None:
        """The label of an end is `<device>-<interface>`, the text DcimCable.name joins with `__`."""
        assert endpoint_label("leaf-01", "Ethernet1/49") == "leaf-01-Ethernet1/49"

    def test_no_collision_over_the_planner_cables_of_a_large_fabric(self) -> None:
        """Thousands of the cables the cabling planner really produces get distinct IDs.

        12 DCs x 4 pods x (32 leaves with 4 uplinks to 4 spines): 6 144 leaf-spine cables, plus the 12 x 4 x 8
        border-leaf to firewall cables, named exactly as connections.py names them.
        """
        names: list[list[str]] = []
        for dc in range(1, 13):
            for pod in range(1, 5):
                prefix = f"dc{dc}-pod{pod}"
                leaves = [
                    MockInterface(f"Ethernet1/{port}", f"{prefix}-leaf-{leaf:02d}", _QSFP28)
                    for leaf in range(1, 33)
                    for port in range(49, 53)
                ]
                spines = [
                    MockInterface(f"Ethernet1/{port}", f"{prefix}-spine-{spine:02d}", _QSFP28)
                    for spine in range(1, 5)
                    for port in range(1, 33)
                ]
                plan = CablingPlanner(leaves, spines).build_cabling_plan(scenario="rack", cabling_offset=0)
                for src, dst in plan:
                    names.append(
                        [
                            endpoint_label(str(src.device.display_label), str(src.name.value)),
                            endpoint_label(str(dst.device.display_label), str(dst.name.value)),
                        ]
                    )
        assert len(names) >= 5000
        cable_names = {"__".join(sorted(labels)) for labels in names}
        assert len(cable_names) == len(names), "the planner named two cables alike"
        ids = {cable_id(labels) for labels in names}
        assert len(ids) == len(cable_names)


class TestCableAttributes:
    """cable_attributes: what a creation site merges into the cable data."""

    def test_all_three_when_known(self) -> None:
        """An aqua multi-mode cable inside one rack: ID, colour and the intra-rack length."""
        attributes = cable_attributes("mmf", ["a-1", "b-1"], _rack(1, 1), _rack(1, 1))
        assert attributes == {"cable_id": cable_id(["a-1", "b-1"]), "color": "aqua", "length_cm": 100}

    def test_unknown_length_and_colour_are_omitted_not_none(self) -> None:
        """The keys are absent (never None), so a create sends only what is known."""
        attributes: dict[str, Any] = cable_attributes("coax", ["a-1", "b-1"], None, _rack())
        assert set(attributes) == {"cable_id"}

    def test_rerun_gives_the_same_attributes(self) -> None:
        """Idempotency of the derivation itself."""
        first = cable_attributes("smf", ["a-1", "b-1"], _rack(1, 1), _rack(2, 4))
        assert first == cable_attributes("smf", ["b-1", "a-1"], _rack(1, 1), _rack(2, 4))


class TestSuiteLayoutData:
    """The suites of the shipped data keep the schema defaults the length derivation relies on."""

    def test_the_schema_defaults_are_the_documented_pitches(self) -> None:
        """rack_pitch_cm 60 and row_pitch_cm 300 are defaults of LocationSuite, so a suite that omits them has both."""
        schema = yaml.safe_load((ROOT / "schemas/extensions/location/location.yml").read_text())
        suite = next(node for node in schema["nodes"] if node["name"] == "Suite")
        defaults = {a["name"]: a.get("default_value") for a in suite["attributes"]}
        assert defaults["rack_pitch_cm"] == 60 and defaults["row_pitch_cm"] == 300

    def test_every_suite_that_sets_a_pitch_sets_a_positive_integer(self) -> None:
        """Pitches are optional in data (the default applies); one that is given must be a positive integer."""
        suites = declared("LocationSuite", ROOT / "data")
        assert suites
        for suite in suites:
            for name in ("rack_pitch_cm", "row_pitch_cm"):
                if name in suite.item:
                    value = suite.item[name]
                    assert isinstance(value, int) and not isinstance(value, bool) and value >= 1, (
                        f"{suite.item.get('shortname')} in {suite.path.relative_to(ROOT)}: {name}={value!r}"
                    )

    def test_every_suite_is_loadable_with_its_layout(self) -> None:
        """Every LocationSuite in the data states its grid (rows, racks_per_row), the rest comes from defaults."""
        suites = declared("LocationSuite", ROOT / "data")
        assert all(isinstance(s.item.get("rows"), int) and isinstance(s.item.get("racks_per_row"), int) for s in suites)
