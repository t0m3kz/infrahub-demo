"""Unit tests for generators/helpers/security_zone.py::ensure_security_zone."""

from __future__ import annotations

import asyncio
from typing import Any, cast
from unittest.mock import AsyncMock, MagicMock

import pytest

from checks.segment_security import CheckSegmentSecurity
from generators.helpers.security_zone import ensure_security_zone
from generators.protocols import SecurityZone
from utils.exchange_transit import ZONE_BY_NS_TYPE


def _client(zone_id: str = "zone-1") -> Any:
    """A client whose zone lookup finds one zone and whose create() returns a saveable node."""
    client = MagicMock()
    zone = MagicMock(id=zone_id)
    client.filters = AsyncMock(return_value=[zone])
    segment_obj = MagicMock()
    segment_obj.save = AsyncMock()
    client.create = AsyncMock(return_value=segment_obj)
    client.segment_obj = segment_obj
    return client


def _run(client: Any, **overrides: Any) -> str | None:
    kwargs: dict[str, Any] = {"kind": "ManagedVlanSegment", "segment_id": "seg-1", "environment": "p"}
    kwargs.update(overrides)
    return asyncio.run(ensure_security_zone(client, **kwargs))


class TestEnsureSecurityZone:
    """The zone is derived from the segment's environment and written with its real kind."""

    @pytest.mark.parametrize("kind", ["ManagedVlanSegment", "ManagedVxlanSegment", "CloudNetworkSegment"])
    def test_writes_the_zone_with_the_segments_own_kind_untracked(self, kind: str) -> None:
        """Every non-External kind is written under its own typename, outside the run's group."""
        client = _client()

        assert _run(client, kind=kind) == "PROD-ZONE"

        client.filters.assert_awaited_once_with(kind=SecurityZone, name__value="PROD-ZONE")
        client.create.assert_awaited_once_with(kind=kind, data={"id": "seg-1", "security_zone": {"id": "zone-1"}})
        client.segment_obj.save.assert_awaited_once_with(allow_upsert=True, update_group_context=False)

    @pytest.mark.parametrize("environment", ["n", "s", "d", "t"])
    def test_non_production_environments_map_to_the_nonprod_zone(self, environment: str) -> None:
        client = _client()

        assert _run(client, environment=environment) == "NONPROD-ZONE"

        client.filters.assert_awaited_once_with(kind=SecurityZone, name__value="NONPROD-ZONE")

    def test_second_pass_writes_nothing(self) -> None:
        """Same segment, same run: no lookup and no write the second time."""
        client = _client()

        _run(client)
        _run(client)

        assert client.filters.await_count == 1
        assert client.create.await_count == 1

    def test_segment_already_in_the_zone_is_not_written(self) -> None:
        """A segment read back with its zone already right causes neither lookup nor write."""
        client = _client()

        assert _run(client, current_zone="PROD-ZONE") == "PROD-ZONE"

        client.filters.assert_not_awaited()
        client.create.assert_not_awaited()

    def test_wrong_current_zone_is_corrected(self) -> None:
        client = _client()

        _run(client, current_zone="NONPROD-ZONE")

        client.create.assert_awaited_once()

    def test_zone_lookup_is_memoised_across_segments(self) -> None:
        client = _client()

        _run(client, segment_id="seg-1")
        _run(client, segment_id="seg-2")

        assert client.filters.await_count == 1
        assert client.create.await_count == 2

    def test_memo_does_not_leak_between_runs(self) -> None:
        """A new client (a new run) looks the zone up again."""
        first, second = _client(), _client()

        _run(first)
        _run(second)

        second.filters.assert_awaited_once()
        second.create.assert_awaited_once()

    def test_external_segment_is_never_touched(self) -> None:
        """An External segment's zone is data: no lookup, no write, no zone name."""
        client = _client()

        assert _run(client, kind="ManagedExternalSegment") is None

        client.filters.assert_not_awaited()
        client.create.assert_not_awaited()

    @pytest.mark.parametrize("zone", ["MANAGEMENT-ZONE", "INTERNET-ZONE"])
    @pytest.mark.parametrize("environment", ["p", "d", None])
    def test_segment_in_an_exchange_zone_keeps_it(self, zone: str, environment: str | None) -> None:
        """A shared service segment (MANAGEMENT services) is not re-zoned to its environment's zone.

        The same exemption the segment_security check makes: an app with a
        component on the infra segment used to move it to PROD-ZONE.
        """
        client = _client()

        assert _run(client, current_zone=zone, environment=environment) == zone

        client.filters.assert_not_awaited()
        client.create.assert_not_awaited()

    @pytest.mark.parametrize("zone", sorted(ZONE_BY_NS_TYPE.values()))
    def test_helper_keeps_exactly_the_zones_the_check_accepts(self, zone: str) -> None:
        """For a dev segment found in ``zone``, the helper leaves the zone as it is if and
        only if the segment_security check accepts it (one predicate, no disagreement)."""
        check = cast(Any, CheckSegmentSecurity.__new__(CheckSegmentSecurity))
        errors: list[str] = []
        check.log_error = lambda message: errors.append(message)
        check._validate_zone(
            {"typename": "ManagedVlanSegment", "name": "s", "environment": "d", "security_zone": {"name": zone}}
        )

        kept = _run(_client(), current_zone=zone, environment="d") == zone

        assert kept == (not errors)

    @pytest.mark.parametrize("kind", [None, ""])
    def test_missing_kind_is_an_error_not_a_default(self, kind: str | None) -> None:
        client = _client()

        with pytest.raises(ValueError, match="typename"):
            _run(client, kind=kind)

        client.create.assert_not_awaited()

    @pytest.mark.parametrize("environment", [None, ""])
    def test_missing_environment_fails_closed(self, environment: str | None) -> None:
        """No guess of production: nothing is looked up or written."""
        client = _client()

        with pytest.raises(ValueError, match="environment"):
            _run(client, environment=environment)

        client.filters.assert_not_awaited()
        client.create.assert_not_awaited()

    def test_missing_segment_id_is_an_error(self) -> None:
        with pytest.raises(ValueError, match="segment id"):
            _run(_client(), segment_id=None)

    def test_missing_zone_raises_and_writes_nothing(self) -> None:
        """The zones are bootstrap data: a missing one is a setup error, never created here."""
        client = _client()
        client.filters = AsyncMock(return_value=[])

        with pytest.raises(LookupError, match="PROD-ZONE"):
            _run(client)

        client.create.assert_not_awaited()

    def test_write_failure_propagates_and_is_not_memoised(self) -> None:
        """A failed write is retried by the next call, not remembered as done."""
        client = _client()
        client.segment_obj.save = AsyncMock(side_effect=[RuntimeError("boom"), None])

        with pytest.raises(RuntimeError, match="boom"):
            _run(client)
        assert _run(client) == "PROD-ZONE"

        assert client.segment_obj.save.await_count == 2


def test_application_query_selects_segment_typename() -> None:
    """The app loop zones segments by their real kind: the query must select it (no default kind exists)."""
    from pathlib import Path

    text = Path(__file__).resolve().parents[2].joinpath("queries/topology/add/application.gql").read_text()
    fragment = text.split("fragment SegmentSecurityFields", 1)[1].split("\n}\n", 1)[0]

    assert "__typename" in fragment
