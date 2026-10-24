#!/usr/bin/env python3
"""Generate smoke test fixtures (input.json + output.txt) for all config transforms.

Usage:
    PYTHONPATH=. uv run python tests/smoke/generate_config_fixtures.py

Creates directories under tests/smoke/configs/ with input.json and output.txt
for each device-type × platform × scenario combination.

Scenarios:
    ebgp_ibgp  — eBGP underlay + iBGP overlay (separate ASNs)
    ospf_ibgp  — OSPF underlay + iBGP overlay (single shared ASN)

The builders live in tests/smoke/fixture_builders/, one module per device family; each
exposes the writers of its fixtures, which this entry point runs in turn.
"""

from __future__ import annotations

from collections.abc import Callable

from tests.smoke.fixture_builders import (
    cisco_xe,
    cloud,
    edge,
    fabric,
    firewall,
    group_policy,
    mlag,
    office,
    proxy_lb,
    routing_identity,
    transit,
)
from tests.smoke.fixture_builders.common import SMOKE_DIR

# (heading printed before the writer or None when the writer prints its own, writer -> (generated, errors)).
WRITERS: list[tuple[str | None, Callable[[], tuple[int, int]]]] = [
    (None, fabric.write_fixtures),
    ("Catalyst 9000 (cisco_xe) fixtures", cisco_xe.write_cisco_xe_fixtures),
    ("Catalyst 9000 (cisco_xe) office LISP site fixtures", office.write_office_lisp_fixtures),
    ("Catalyst 9000 (cisco_xe) office WAN exit fixtures", office.write_office_wan_exit_fixtures),
    ("Catalyst 9000 (cisco_xe) segment ACL and exchange static fixtures", cisco_xe.write_cisco_xe_acl_fixtures),
    (None, transit.write_fixtures),
    (None, edge.write_fixtures),
    (None, firewall.write_fixtures),
    (None, mlag.write_fixtures),
    (None, proxy_lb.write_fixtures),
    (None, group_policy.write_fixtures),
    (None, routing_identity.write_fixtures),
    ("Cloud VPC Terraform fixtures", cloud.write_fixtures),
]


def main() -> int:
    """Write every fixture; return 1 when any of them failed to render."""
    SMOKE_DIR.mkdir(parents=True, exist_ok=True)

    generated = errors = 0
    for heading, write in WRITERS:
        if heading:
            print(f"\nGenerating {heading}:")
        g, e = write()
        generated += g
        errors += e

    print(f"\nDone: {generated} generated, {errors} errors")
    # A fixture that fails to render writes no file, so it leaves no diff for the
    # CI staleness check to catch — the failure has to surface here instead. That
    # is how a broken `ipaddr` filter in cisco_wsa_asyncos.j2 went unnoticed.
    return 1 if errors else 0


if __name__ == "__main__":
    raise SystemExit(main())
