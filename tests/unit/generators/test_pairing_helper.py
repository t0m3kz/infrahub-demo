"""Unit tests for generators.helpers.pairing.pair_device_names()."""

from __future__ import annotations

from generators.helpers.pairing import pair_device_names


def test_empty_list_returns_no_pairs() -> None:
    assert pair_device_names([]) == []


def test_single_device_returns_no_pairs() -> None:
    assert pair_device_names(["a"]) == []


def test_two_devices_returns_one_sorted_pair() -> None:
    assert pair_device_names(["b", "a"]) == [("a", "b")]


def test_even_count_pairs_all_devices() -> None:
    assert pair_device_names(["d", "c", "b", "a"]) == [("a", "b"), ("c", "d")]


def test_odd_count_leaves_last_device_unpaired() -> None:
    assert pair_device_names(["c", "a", "b"]) == [("a", "b")]
