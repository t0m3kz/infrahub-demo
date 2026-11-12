"""The text fields of a change request: caps the facts, renders one template per field (templates/change/)."""

from __future__ import annotations

from collections import Counter, defaultdict
from collections.abc import Sequence
from dataclasses import dataclass
from pathlib import Path
from typing import Any

from jinja2 import StrictUndefined

from transforms.helpers.templates import load_template
from utils.change_facts import NO_SITE, ChangeFacts, DeviceChange, build_facts
from utils.change_rules import (
    DESCRIPTION_MAX_CHARS,
    DEVICE_MAX_ITEMS,
    DIFF_TRUNCATED_NOTE,
    MAINTENANCE_TZ,
    REMOVED,
    SECTION_MAX_ITEMS,
    STATUS_ORDER,
    ChangeInputs,
    artifact_fingerprint,
    change_type,
    conflicts,
    diff_counts,
    impact,
    ordered_targets,
    planned_window,
    risk_level,
    risk_score,
)

TEMPLATES = Path(__file__).resolve().parents[1] / "templates" / "change"
TEXT_FIELDS = {
    "short_description": "short_description.j2",
    "description": "description.j2",
    "implementation_plan": "implementation_plan.j2",
    "backout_plan": "backout_plan.j2",
    "test_plan": "test_plan.j2",
    "communication_plan": "communication_plan.j2",
}
STATUS_WORDS = {"ADDED": "Added", "UPDATED": "Updated", "REMOVED": "Removed"}
SUMMARY_VERBS = {"ADDED": "Adds", "UPDATED": "Updates", "REMOVED": "Removes"}
FAMILY_NOUNS = ("network segment", "firewall rule", "cable", "carrier circuit", "session circuit")


@dataclass(frozen=True)
class Group:
    """The items of one status in a section."""

    word: str
    items: tuple[Any, ...]


@dataclass(frozen=True)
class Section:
    """A capped list of items grouped by status; ``more`` items did not fit."""

    total: int
    groups: tuple[Group, ...] = ()
    more: int = 0


@dataclass(frozen=True)
class Role:
    """The devices of one role in a site."""

    name: str
    devices: tuple[DeviceChange, ...]


@dataclass(frozen=True)
class Site:
    """The devices of one site, by role."""

    name: str
    roles: tuple[Role, ...]


@dataclass(frozen=True)
class DeviceSection:
    """The affected devices by site then role; ``more`` devices did not fit."""

    total: int
    sites: tuple[Site, ...] = ()
    more: int = 0


def limit_chars(text: str, limit: int) -> str:
    """``text`` cut at ``limit`` characters (the cut is marked by ``...``)."""
    return text if len(text) <= limit else text[: limit - 4].rstrip() + "\n..."


def _noun(count: int, noun: str) -> str:
    return f"{count} {noun}" if count == 1 else f"{count} {noun}s"


def section(items: Sequence[Any], cap: int = SECTION_MAX_ITEMS) -> Section:
    """The first ``cap`` items (already in display order) grouped by status."""
    shown = items[:cap]
    groups = tuple(
        Group(STATUS_WORDS[status], tuple(i for i in shown if i.status == status))
        for status in STATUS_ORDER
        if any(i.status == status for i in shown)
    )
    return Section(total=len(items), groups=groups, more=len(items) - len(shown))


def device_section(devices: Sequence[DeviceChange], cap: int = DEVICE_MAX_ITEMS) -> DeviceSection:
    """The first ``cap`` devices (sorted by site, role, name) grouped by site then role."""
    shown = devices[:cap]
    by_site: dict[str, dict[str, list[DeviceChange]]] = defaultdict(lambda: defaultdict(list))
    for device in shown:
        by_site[device.site][device.role or "no role"].append(device)
    sites = tuple(
        Site(name, tuple(Role(role, tuple(members)) for role, members in roles.items()))
        for name, roles in by_site.items()
    )
    return DeviceSection(total=len(devices), sites=sites, more=len(devices) - len(shown))


def _summary(facts: ChangeFacts) -> list[dict[str, Any]]:
    """One entry per status: the verb and what it applies to ("2 network segments", "3 firewall rules")."""
    families = {
        "network segment": facts.segments,
        "firewall rule": facts.rules,
        "cable": [link for link in facts.links if link.family == "cable"],
        "carrier circuit": [link for link in facts.links if link.family == "carrier circuit"],
        "session circuit": [link for link in facts.links if link.family == "session circuit"],
    }
    entries = []
    for status in STATUS_ORDER:
        parts = [
            _noun(count, noun)
            for noun in FAMILY_NOUNS
            if (count := sum(1 for item in families[noun] if item.status == status))
        ]
        if parts:
            entries.append({"verb": SUMMARY_VERBS[status], "parts": parts})
    return entries


def _config_parts(facts: ChangeFacts) -> list[str]:
    """``3 new``, ``2 updated``... : the device configurations by what happens to them."""
    order = ("new", "updated", "removed")
    counts = {action: sum(1 for d in facts.devices if d.action == action) for action in order}
    return [f"{counts[action]} {action}" for action in order if counts[action]]


def _others(inputs: ChangeInputs, counts: dict[str, int]) -> dict[str, int]:
    """The changed objects the diff counts but the text does not describe, by status (none when the read was cut)."""
    if inputs.objects_truncated:
        return {}
    described = Counter(o.status for o in inputs.objects)
    return {status: n for status in STATUS_ORDER if (n := counts[status] - described[status]) > 0}


def build_context(inputs: ChangeInputs) -> dict[str, Any]:
    """Everything the templates read (StrictUndefined)."""
    facts = build_facts(inputs.rows, inputs.artifacts, inputs.objects)
    counts = diff_counts(inputs)
    devices = {d.name: d for d in facts.devices}
    links = [link for link in facts.links if link.status != REMOVED]
    removed = [
        item.name for items in (facts.segments, facts.rules, facts.links) for item in items if item.status == REMOVED
    ]
    return {
        "pc_name": inputs.pc_name,
        "pc_description": (inputs.pc_description or "").strip(),
        "counts": counts,
        "configs": len(inputs.artifacts),
        "truncated_note": DIFF_TRUNCATED_NOTE if inputs.objects_truncated else "",
        "max_chars": DESCRIPTION_MAX_CHARS,
        "customers": sorted(inputs.customers),
        "sites": sorted({d.site for d in facts.devices if d.site != NO_SITE}),
        "summary": _summary(facts),
        "config_parts": _config_parts(facts),
        "devices": device_section(facts.devices),
        "links": section(facts.links),
        "rules": section(facts.rules),
        "segments": section(facts.segments),
        "others": _others(inputs, counts),
        "rollout": [devices[t] for t in ordered_targets(inputs) if t in devices],
        "verify_devices": sorted(inputs.devices, key=lambda d: d.name)[:DEVICE_MAX_ITEMS],
        "verify_devices_more": max(0, len(inputs.devices) - DEVICE_MAX_ITEMS),
        "verify_links": section(links),
        "verify_segments": section([s for s in facts.segments if s.status != REMOVED]),
        "removed": removed[:SECTION_MAX_ITEMS],
        "removed_more": max(0, len(removed) - SECTION_MAX_ITEMS),
        "any_removed": bool(counts[REMOVED]),
    }


def render_field(inputs: ChangeInputs, field_name: str, context: dict[str, Any] | None = None) -> str:
    """One text field, rendered from its template."""
    template = load_template(
        str(TEMPLATES),
        TEXT_FIELDS[field_name],
        filters={"limit_chars": limit_chars},
        keep_trailing_newline=False,
        trim_blocks=True,
        lstrip_blocks=True,
        undefined=StrictUndefined,
    )
    return template.render(**(context if context is not None else build_context(inputs))).strip()


PC_COMMENT_TEMPLATE = "pc_comment.j2"
PC_COMMENT_TIME = "%a %d %b %H:%M"


def render_pc_comment(
    name: str, url: str, derived: dict[str, Any], risk: str | None, devices: int, ticket: str | None
) -> str:
    """The PC thread comment: the request's link, type, effective risk, devices, window and ticket."""
    start, end = derived.get("planned_start"), derived.get("planned_end")
    template = load_template(
        str(TEMPLATES), PC_COMMENT_TEMPLATE, trim_blocks=True, lstrip_blocks=True, undefined=StrictUndefined
    )
    return template.render(
        name=name,
        url=url,
        change_type=derived["change_type"],
        risk=risk,
        score=derived["risk_score"],
        devices=devices,
        start=start.astimezone(MAINTENANCE_TZ).strftime(PC_COMMENT_TIME) if start else None,
        end=end.astimezone(MAINTENANCE_TZ).strftime(PC_COMMENT_TIME) if end else None,
        tz=MAINTENANCE_TZ.key,
        ticket=ticket,
    ).strip()


def derive_change_request(inputs: ChangeInputs) -> dict[str, Any]:
    """Every derived field of the change request, keyed by its attribute name."""
    score = risk_score(inputs)
    risk = risk_level(score)
    kind = change_type(inputs, risk)
    start, end = planned_window(inputs, kind)
    context = build_context(inputs)
    return {
        **{name: render_field(inputs, name, context) for name in TEXT_FIELDS},
        "change_type": kind,
        "risk": risk,
        "risk_score": score,
        "impact": impact(inputs),
        "conflicts": conflicts(inputs) or None,
        "planned_start": start,
        "planned_end": end,
        "artifact_fingerprint": artifact_fingerprint(inputs.artifacts),
    }
