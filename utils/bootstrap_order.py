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
from pathlib import Path

# Object files only. A directory may also hold READMEs or other non-data files,
# which infrahubctl would not load either.
OBJECT_FILE_SUFFIXES = (".yml", ".yaml")

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
