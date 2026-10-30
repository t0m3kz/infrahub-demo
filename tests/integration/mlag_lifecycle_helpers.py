"""The pure readers and judges of test_66's MLAG scenario (test_05), and the reads that feed them.

Option (d) of the VLAN rows: a ManagedVlanDomainSegment points straight at its DEVICE, so the ManagedMLAG pairing two
access-leafs owns no row. Deleting the MLAG, and building it again, must therefore leave every row (id, VLAN id, SVI)
alone. These functions say what changed; tests/unit/test_integration_mlag_lifecycle.py runs them on canned payloads.
"""

from __future__ import annotations

import re
from pathlib import Path
from typing import Any

from infrahub_sdk import InfrahubClient

from checks.mlag_domains import LAG_KIND, ROW_KIND, mlag_lag_errors, pair_row_errors
from utils.data_cleaning import clean_data

ACCESS_LEAF_ROLE = "access-leaf"
MLAG_KIND = "ManagedMLAG"
SVI_ROLE = "svi"
MLAG_MEMBERS = 2

# Row: device id -> {segment deployment id -> {id, vlan_id, svi_id}}; SVIs: device id -> {interface name -> id}.
Rows = dict[str, dict[str, dict[str, Any]]]
Svis = dict[str, dict[str, str]]

MLAG_CANDIDATES_QUERY = """
query {
  ManagedMLAG {
    edges {
      node {
        id
        name { value }
        capabilities {
          edges {
            node {
              id
              name { value }
              ... on DcimPhysicalDevice { role { value } rack { node { id } } }
            }
          }
        }
      }
    }
  }
}
"""

ROWS_QUERY = """
query ($devices: [ID]) {
  ManagedVlanDomainSegment(device__ids: $devices) {
    edges {
      node {
        id
        vlan_id { value }
        device { node { id } }
        segment_deployment { node { id } }
        svi { node { id } }
      }
    }
  }
  DcimVirtualInterface(device__ids: $devices, role__value: "svi") {
    edges { node { id name { value } device { node { id } } } }
  }
}
"""

_MLAG_GQL = Path(__file__).parent.parent.parent / "queries" / "validation" / "mlag_domains.gql"
# Lines of a rendered access-leaf that name a VLAN of a segment, per platform. The first group is the VLAN id (an
# EOS ``vlan 100`` / ``interface Vlan100`` / ``   vxlan vlan 100 vni 10100``); ``vn-segment`` (NX-OS) and ``member vni``
# carry a VNI and no VLAN id, and nothing of an MLAG's own config uses them.
_VLAN_LINE = re.compile(r"^\s*(?:vlan|interface Vlan|vxlan vlan) ?(\d+)\b")
_VNI_LINE = re.compile(r"^\s*(?:vn-segment|member vni) \d+")


def _node(edge: dict[str, Any]) -> dict[str, Any]:
    return edge["node"]


def _id(holder: dict[str, Any], key: str) -> str | None:
    return ((holder.get(key) or {}).get("node") or {}).get("id")


def parse_candidates(result: dict[str, Any]) -> list[dict[str, Any]]:
    """Every MLAG of a ``MLAG_CANDIDATES_QUERY`` result: {id, name, members: [{id, name, role, rack_id}]}."""
    candidates = []
    for edge in (result.get(MLAG_KIND) or {}).get("edges", []):
        node = _node(edge)
        members = [
            {
                "id": member["id"],
                "name": (member.get("name") or {}).get("value"),
                "role": (member.get("role") or {}).get("value"),
                "rack_id": _id(member, "rack"),
            }
            for member in (_node(e) for e in (node.get("capabilities") or {}).get("edges", []))
        ]
        candidates.append({"id": node["id"], "name": node["name"]["value"], "members": members})
    return sorted(candidates, key=lambda candidate: candidate["name"])


def parse_rows(result: dict[str, Any]) -> tuple[Rows, Svis]:
    """The rows and the role-svi interfaces of a ``ROWS_QUERY`` result, grouped by device id."""
    rows: Rows = {}
    for edge in (result.get("ManagedVlanDomainSegment") or {}).get("edges", []):
        node = _node(edge)
        device, deployment = _id(node, "device"), _id(node, "segment_deployment")
        if device and deployment:
            rows.setdefault(device, {})[deployment] = {
                "id": node["id"],
                "vlan_id": (node.get("vlan_id") or {}).get("value"),
                "svi_id": _id(node, "svi"),
            }
    svis: Svis = {}
    for edge in (result.get("DcimVirtualInterface") or {}).get("edges", []):
        node = _node(edge)
        if device := _id(node, "device"):
            svis.setdefault(device, {})[str(node["name"]["value"])] = node["id"]
    return rows, svis


def candidate_errors(candidate: dict[str, Any], rows: Rows) -> list[str]:
    """Why ``candidate`` cannot show the (d) property: two access-leaf members, racked, each with at least one row."""
    errors = []
    members = candidate["members"]
    if len(members) != MLAG_MEMBERS:
        errors.append(f"{candidate['name']} has {len(members)} member(s), expected {MLAG_MEMBERS}")
    for member in members:
        if member["role"] != ACCESS_LEAF_ROLE:
            errors.append(f"{member['name']} is a {member['role']}, expected {ACCESS_LEAF_ROLE}")
        if not member["rack_id"]:
            errors.append(f"{member['name']} is in no rack: add_rack cannot rebuild its pair")
        if not rows.get(member["id"]):
            errors.append(f"{member['name']} has no ManagedVlanDomainSegment row")
    return errors


def pick_candidate(candidates: list[dict[str, Any]], rows: Rows) -> tuple[dict[str, Any] | None, list[str]]:
    """The first MLAG (by name) that passes ``candidate_errors``, or None with why each one failed."""
    reasons = []
    for candidate in candidates:
        errors = candidate_errors(candidate, rows)
        if not errors:
            return candidate, []
        reasons.append(f"{candidate['name']}: " + "; ".join(errors))
    return None, reasons or ["no ManagedMLAG exists"]


def snapshot_errors(before: tuple[Rows, Svis], after: tuple[Rows, Svis], names: dict[str, str]) -> list[str]:
    """What changed between two (rows, svis) snapshots of the pair's devices: a row added, deleted, re-created (new id),
    renumbered, or pointing at another SVI; an SVI added, deleted or re-created. ``names``: device id -> name."""
    errors: list[str] = []
    for device_id, name in sorted(names.items(), key=lambda item: item[1]):
        old_rows, new_rows = before[0].get(device_id, {}), after[0].get(device_id, {})
        errors += [f"{name}: row of deployment {dep} was deleted" for dep in sorted(old_rows.keys() - new_rows.keys())]
        errors += [f"{name}: row of deployment {dep} appeared" for dep in sorted(new_rows.keys() - old_rows.keys())]
        errors += [
            f"{name}: row of deployment {dep} changed: {old_rows[dep]} -> {new_rows[dep]}"
            for dep in sorted(old_rows.keys() & new_rows.keys())
            if old_rows[dep] != new_rows[dep]
        ]
        old_svis, new_svis = before[1].get(device_id, {}), after[1].get(device_id, {})
        errors += [f"{name}: SVI {svi} was deleted" for svi in sorted(old_svis.keys() - new_svis.keys())]
        errors += [f"{name}: SVI {svi} appeared" for svi in sorted(new_svis.keys() - old_svis.keys())]
        errors += [
            f"{name}: SVI {svi} was re-created: {old_svis[svi]} -> {new_svis[svi]}"
            for svi in sorted(old_svis.keys() & new_svis.keys())
            if old_svis[svi] != new_svis[svi]
        ]
    return errors


def vlan_lines(rendered: str, vlan_ids: set[Any]) -> list[str]:
    """The lines of a rendered config that carry a segment's VLAN or VNI, in order: ``vlan N``, ``interface VlanN`` and
    ``vxlan vlan N vni M`` for the ids in ``vlan_ids`` (the MLAG's own VLAN, for example the peer-link's 4094, is not
    one), and every ``vn-segment`` / ``member vni`` line. The rest of the config (the MLAG / vPC domain block, the
    peer-link) is not part of this subset."""
    wanted = {str(vlan_id) for vlan_id in vlan_ids}
    lines = []
    for line in rendered.splitlines():
        match = _VLAN_LINE.match(line)
        if (match and match.group(1) in wanted) or _VNI_LINE.match(line):
            lines.append(line.rstrip())
    return lines


def artifact_errors(before: dict[str, list[str]], after: dict[str, list[str]]) -> list[str]:
    """Why a device's VLAN/SVI lines (``vlan_lines``) differ; an empty ``before`` means the render proves nothing."""
    errors = [
        f"{device}: no VLAN line was rendered before: the artifact proves nothing"
        for device, lines in before.items()
        if not lines
    ]
    for device in sorted(before):
        if before[device] != after.get(device):
            gone = [line for line in before[device] if line not in (after.get(device) or [])]
            new = [line for line in (after.get(device) or []) if line not in before[device]]
            errors.append(f"{device}: VLAN/SVI lines changed (lost {gone}, gained {new})")
    return errors


def mlag_check_findings(result: dict[str, Any]) -> list[str]:
    """The M4 (pair rows) and M5 (port-channels) findings of checks/mlag_domains.py on a ``mlag_domains.gql`` result."""
    cleaned = clean_data(result)
    domains = cleaned.get(MLAG_KIND) or []
    return pair_row_errors(domains, cleaned.get(ROW_KIND) or []) + mlag_lag_errors(domains, cleaned.get(LAG_KIND) or [])


def new_findings(before: list[str], after: list[str]) -> list[str]:
    """The findings of ``after`` that ``before`` did not have (the stack may carry earlier ones)."""
    return sorted(set(after) - set(before))


async def fetch_candidates(client: InfrahubClient, branch: str) -> list[dict[str, Any]]:
    """Every MLAG of the branch with its members."""
    return parse_candidates(await client.execute_graphql(query=MLAG_CANDIDATES_QUERY, branch_name=branch))


async def fetch_rows(client: InfrahubClient, branch: str, device_ids: list[str]) -> tuple[Rows, Svis]:
    """The rows and role-svi interfaces of ``device_ids`` on the branch."""
    result = await client.execute_graphql(query=ROWS_QUERY, variables={"devices": device_ids}, branch_name=branch)
    return parse_rows(result)


async def fetch_mlag_findings(client: InfrahubClient, branch: str) -> list[str]:
    """Run the query of validate_mlag_domains and judge it with the check's own M4 / M5 functions."""
    result = await client.execute_graphql(query=_MLAG_GQL.read_text(), branch_name=branch)
    return mlag_check_findings(result)
