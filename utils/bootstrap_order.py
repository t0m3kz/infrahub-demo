"""Dependency ordering for Infrahub object files.

``infrahubctl object load <dir>`` loads every file in the directory as one
concurrent run. That is fine for objects that stand alone, but the bootstrap
data is layered: ``05b_software_images.yml`` references a ``platform`` created
by ``05_platforms.yml``, ``06_device_types.yml`` references a ``manufacturer``
from ``04_manufacturers.yml``, and all 30 device-template files reference an
``owner``, ``platform``, ``software_image`` and ``device_type`` from the four
files below them. Those references resolve by HFID, and an HFID lookup only
succeeds once the referenced node is visible to the query — which is not
guaranteed at the moment its own upsert returns. Loading everything at once
races referencing objects against the nodes they depend on, and the run fails
partway with::

    ['DcimSoftwareImageUpsert'] Unable to find the node arista_eos /
    DcimPlatform in the database.

Intermittently, which is worse than slowly: the same directory loads cleanly on
a retry, so the failure looks like flakiness rather than missing ordering.

The data already encodes the ordering. Every bootstrap file carries a numeric
prefix (``02_providers``, ``04_manufacturers``, ``05_platforms``,
``05b_software_images``, ``06_device_types``, ``10_physical_devices_*``) and the
convention is that a higher prefix may reference a lower one, never the reverse.
This module turns that convention into an executable barrier: one tier per
prefix, tiers loaded in ascending order, each invocation finishing before the
next begins. Files sharing a prefix are declared independent of each other by
that convention, so they still load together at full concurrency — the 19
``10_*`` template files reference only tiers below them, never one another.

Splitting one invocation into ~24 costs about 0.4s each in CLI startup, ~10s
total. That buys determinism on the layer every later object depends on.
"""

from __future__ import annotations

import re
from collections.abc import Sequence
from pathlib import Path
from typing import Any

import yaml

# Object files only. A directory may also hold READMEs or other non-data files,
# which infrahubctl would not load either.
OBJECT_FILE_SUFFIXES = (".yml", ".yaml")

# Concurrency for a tier whose files nest child objects inline.
#
# Splitting one directory load into per-tier loads concentrates concurrency.
# Before tiers, a budget of 10 was shared across all 52 files; after, tier 11's
# 19 template files get that budget to themselves, so more parent-then-children
# groups run at once than before. Those files declare interfaces inside their
# template device, so every parent is followed immediately by children that
# reference it, and the load fails with "Unable to find the node <uuid> /
# TemplateDcimDevice in the database" — a UUID, not an HFID, so the loader holds
# a valid id and the write simply has not landed yet.
#
# Serialising these tiers puts the write pressure below the pre-tier baseline
# rather than above it, which is the pressure the split introduced. It is not a
# proof against the lag: a device's own children still follow it directly. But
# the race is load-sensitive — concurrency 30 was observed to fail on the
# Edgecore SONiC templates and 10 on the NetScaler ones — and load is the part
# this code controls. Flat tiers keep the default; 5 of 24 tiers serialise.
NESTED_TIER_CONCURRENCY = 1


def _relationship_nests_objects(value: Any) -> bool:
    """True if a relationship value carries inline child objects.

    Two shapes count, both from the object file format: a dict holding ``data``
    (``interfaces: {kind:, data: [...]}``) and a list of dicts holding ``data``
    (``devices: [{kind:, data: {...}}]``).
    """
    if isinstance(value, dict):
        return "data" in value
    if isinstance(value, list):
        return any(isinstance(item, dict) and "data" in item for item in value)
    return False


def object_file_nests_children(path: Path) -> bool:
    """True if any object in ``path`` declares child objects inline.

    Detected structurally rather than by filename: a new nested file picks up the
    slower, safer concurrency without anyone remembering to add it to a list.
    """
    try:
        documents = list(yaml.safe_load_all(path.read_text()))
    except (OSError, yaml.YAMLError):
        # Unreadable or malformed YAML is infrahubctl's error to report, with a
        # far better message than anything available here. Assume the safer
        # (serialised) answer and let the load fail on its own terms.
        return True

    for document in documents:
        if not isinstance(document, dict):
            continue
        for entry in (document.get("spec") or {}).get("data") or []:
            if isinstance(entry, dict) and any(_relationship_nests_objects(value) for value in entry.values()):
                return True
    return False


def tier_concurrency(tier: Sequence[Path], default: int) -> int:
    """Concurrency to load ``tier`` with — serialised if any file nests children."""
    if any(object_file_nests_children(path) for path in tier):
        return NESTED_TIER_CONCURRENCY
    return default


# Leading digits, optionally followed by letters used to insert a file between
# two existing tiers (05 -> 05b -> 06).
_PREFIX = re.compile(r"^(\d+)([a-z]*)_")


def _sort_key(path: Path) -> tuple[int, str]:
    """Tier of one file: ``(number, letters)``.

    Sorting on the parsed number rather than the raw string matters — as text,
    ``"5"`` sorts after ``"10"``, so a single-digit prefix would silently land
    in the last tier instead of the fifth.

    Files with no numeric prefix sort last. Nothing can declare a dependency on
    them through a convention they do not follow, and sorting them last keeps
    them loaded rather than skipped.
    """
    match = _PREFIX.match(path.name)
    if not match:
        return (10**6, path.name)
    return (int(match.group(1)), match.group(2))


def bootstrap_load_tiers(directory: Path) -> list[list[Path]]:
    """Group the object files in ``directory`` into dependency tiers.

    Returns one list per distinct filename prefix, in ascending prefix order.
    Files within a tier are sorted by name and may be loaded concurrently.

    Every object file in the directory appears in exactly one tier: dropping a
    file here would leave its objects missing, which surfaces much later as an
    unrelated failure, so totality is the property to preserve rather than
    tidiness of the prefixes.
    """
    files = sorted(
        (path for path in directory.iterdir() if path.is_file() and path.suffix in OBJECT_FILE_SUFFIXES),
        key=_sort_key,
    )

    tiers: list[list[Path]] = []
    current_key: tuple[int, str] | None = None
    for path in files:
        key = _sort_key(path)
        if key != current_key:
            tiers.append([])
            current_key = key
        tiers[-1].append(path)
    return tiers
