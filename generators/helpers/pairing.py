"""Pure two-at-a-time pairing helper shared by HA (firewall/load-balancer) and
MLAG (leaf/tor/l2-leaf/access-leaf) device pairing — both pair same-role
devices into redundant pairs, sorted by name, leaving an odd one out unpaired.
"""

from __future__ import annotations


def pair_device_names(device_names: list[str]) -> list[tuple[str, str]]:
    """Sort device_names and group into (first, second) pairs, two at a time.

    An odd device out is left unpaired (not included in the result) — same
    behavior for any count, not just exactly 2.
    """
    sorted_names = sorted(device_names)
    return list(zip(sorted_names[0::2], sorted_names[1::2]))
