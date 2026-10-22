"""Validate the colocation tree: what each zone and virtual colocation hosts, who serves a footprint, and names.

The schema holds the shape (a zone and a virtual colocation are children of a metro, a footprint's ``serving_zone``
peers a zone); what it cannot hold is held here, on every metro, zone, virtual colocation and footprint at once.
Each rule is a small function over the cleaned query result that returns its findings, so a new rule is one more
entry in ``RULES``:

1. Template kind. A zone is our own hardware in our own cage, so every ``fabric_templates`` entry is a
   TemplateDcimPhysicalDevice; a virtual colocation is a provider-hosted presence (Equinix Network Edge, Megaport
   MCR), so every entry is a TemplateCloudNetworkAppliance (its devices are CloudNetworkAppliance, not
   DcimVirtualDevice). The peer is CoreObjectTemplate, so the schema accepts either.
2. Serving zone. A footprint's ``serving_zone`` is a zone of the footprint's own metro (``parent``): the footprint
   is parented by the metro, the firewall pair that serves it lives in one of its zones.
3. Serving zone firewalls. A serving zone holds firewall devices, else the footprint gets no FirewallContext.
   An unset ``serving_zone`` is not reported: it means no context by design.
4. Unique names. Metro, zone and virtual colocation names are unique across all three kinds: they share the
   device-name space (a zone FR2 names ``eg-fr201``) and the pool-name space.

Design rules (``design`` is optional: a zone, virtual colocation or metro without one is sized with the generator's
fixed fallback, and is only held to these rules where the rule names the design):

5. Kit needs a design. A zone or virtual colocation that declares ``fabric_templates`` has a ``design``: the design
   is the envelope its pools are sized for.
6. Kit within the envelope. The quantities of a role, summed over the ``fabric_templates`` entries, stay within the
   design's ``max_edges`` (edge), ``max_firewalls`` (firewall) or ``max_load_balancers`` (load-balancer).
7. Zone design in the metro envelope. When both the metro and the zone (or virtual colocation) have a design, the
   zone's design is one of the metro design's ``zone_templates``.
8. Zones within the metro envelope. A metro with a design holds at most the design's ``max_zones`` zones and
   virtual colocations together.

Order data (the presence's ``account`` with its ``owner``, and the appliances it hosts; not part of the design
envelope):

9. Terraform order data. A virtual colocation routed to terraform has what the Terraform artifact prints and cannot
   order without: an ``account`` whose ``account_id`` (the billing account number) is set, whose provider is the
   colocation operator above the metro (case-insensitive: operator "EQUINIX", provider "Equinix"), and whose owner has
   at least one notification address, each an e-mail address. Every appliance (CloudNetworkAppliance) of the presence
   is ordered under that account. A router (edge) is an independent appliance with no redundancy representation; a
   firewall pair is an HA domain (a ManagedHA on its ``capabilities`` edge, the same as every other HA pair) whose
   ``mode`` active-passive is the Equinix cluster, and an appliance with no domain is a single device. An appliance is
   in at most one domain; a domain has exactly two members, both appliances of the presence that list it, and the
   mode is active-passive (the artifact renders no other). A pair-level problem is reported once, on the first member
   by name.
"""

from __future__ import annotations

from collections import defaultdict
from collections.abc import Callable
from typing import Any, NamedTuple

from infrahub_sdk.checks import InfrahubCheck

from utils.data_cleaning import clean_data

METRO = "TopologyColocationMetro"
ZONE = "TopologyColocationZone"
VIRTUAL_COLOCATION = "TopologyVirtualColocation"
FOOTPRINT = "TopologyCustomerColocation"
TERRAFORM = "terraform"
"""The infrastructure_orchestrator value that makes a virtual colocation render its Terraform artifact."""

APPLIANCE = "CloudNetworkAppliance"
"""The kind of a device hosted by the provider on a virtual colocation."""

HOSTED_TEMPLATE_KIND = {
    ZONE: "TemplateDcimPhysicalDevice",
    VIRTUAL_COLOCATION: "TemplateCloudNetworkAppliance",
}
"""The template kind each fabric-hosting colocation kind may declare."""

NAMESPACED_KINDS = (METRO, ZONE, VIRTUAL_COLOCATION)
"""The kinds whose names share the device-name and pool-name space."""

FABRIC_KINDS = (ZONE, VIRTUAL_COLOCATION)
"""The kinds that declare kit and carry a zone design (the ColocationFabric generic)."""

ROLE_LIMIT = {"edge": "max_edges", "firewall": "max_firewalls", "load-balancer": "max_load_balancers"}
"""The design attribute that caps the total quantity of each kit role."""


class Finding(NamedTuple):
    """One violated rule, logged against the object that carries it."""

    message: str
    object_id: str | None
    object_type: str


def _name(node: dict[str, Any]) -> str:
    return str(node.get("name") or node.get("id") or "<unnamed>")


def _display_label(owner: dict[str, Any]) -> str:
    """A peer by display_label: the account has no ``name`` and ``name`` lives on OrganizationGeneric, not on the
    OrganizationEntity peer."""
    return str(owner.get("display_label") or owner.get("id") or "<unnamed>")


def _ref_id(reference: Any) -> str | None:
    """The id of a cleaned relationship: a dict with an ``id`` (an id-only selection can also arrive as the bare id)."""
    if isinstance(reference, dict):
        reference = reference.get("id")
    return str(reference) if reference else None


def _entry_label(entry: dict[str, Any]) -> str:
    template = entry.get("template") or {}
    return f"{entry.get('quantity')} x {entry.get('role')} on '{template.get('display_label') or template.get('id')}'"


def template_kind_findings(cleaned: dict[str, Any]) -> list[Finding]:
    """Rule 1: a zone declares physical templates only, a virtual colocation network appliance ones only."""
    findings = []
    for kind, wanted in HOSTED_TEMPLATE_KIND.items():
        for node in cleaned.get(kind) or []:
            for entry in node.get("fabric_templates") or []:
                template = entry.get("template") or {}
                actual = template.get("template_kind")
                if not template or actual == wanted:
                    continue
                findings.append(
                    Finding(
                        f"{kind} '{_name(node)}' declares {_entry_label(entry)}, a {actual or 'template of unknown kind'}"
                        f": its fabric_templates are {wanted} only"
                        + (
                            " (our own hardware in our own cage)."
                            if kind == ZONE
                            else " (a provider-hosted instance, no hardware of ours)."
                        ),
                        node.get("id"),
                        kind,
                    )
                )
    return findings


def serving_zone_metro_findings(cleaned: dict[str, Any]) -> list[Finding]:
    """Rule 2: the serving zone is a zone of the footprint's own metro."""
    findings = []
    for footprint in cleaned.get(FOOTPRINT) or []:
        zone = footprint.get("serving_zone")
        metro = footprint.get("parent") or {}
        if not zone or not metro.get("id"):
            continue
        zone_metro = zone.get("parent") or {}
        if zone_metro.get("id") != metro["id"]:
            findings.append(
                Finding(
                    f"{FOOTPRINT} '{_name(footprint)}' is served by zone '{_name(zone)}' of metro "
                    f"'{zone_metro.get('name') or '<no metro>'}' but is itself in metro '{_name(metro)}': its "
                    "serving_zone is a zone of its own metro.",
                    footprint.get("id"),
                    FOOTPRINT,
                )
            )
    return findings


def serving_zone_firewall_findings(cleaned: dict[str, Any]) -> list[Finding]:
    """Rule 3: a serving zone holds a firewall, else the footprint gets no FirewallContext."""
    findings = []
    for footprint in cleaned.get(FOOTPRINT) or []:
        zone = footprint.get("serving_zone")
        if zone and not zone.get("firewall_devices"):
            findings.append(
                Finding(
                    f"{FOOTPRINT} '{_name(footprint)}' is served by zone '{_name(zone)}', which has no firewall "
                    "device: the footprint would get no FirewallContext. Declare a firewall in the zone's "
                    "fabric_templates or serve the footprint from a zone that has one.",
                    footprint.get("id"),
                    FOOTPRINT,
                )
            )
    return findings


def unique_name_findings(cleaned: dict[str, Any]) -> list[Finding]:
    """Rule 4: metro, zone and virtual colocation names are unique across the three kinds (case-insensitive: the
    device and pool names are lower-cased)."""
    by_name: dict[str, list[tuple[str, dict[str, Any]]]] = defaultdict(list)
    for kind in NAMESPACED_KINDS:
        for node in cleaned.get(kind) or []:
            if node.get("name"):
                by_name[str(node["name"]).casefold()].append((kind, node))
    findings = []
    for owners in by_name.values():
        if len(owners) < 2:
            continue
        for kind, node in owners:
            others = ", ".join(f"{other_kind} '{_name(other)}'" for other_kind, other in owners if other is not node)
            findings.append(
                Finding(
                    f"{kind} '{_name(node)}' shares its name with {others}: colocation metro, zone and virtual "
                    "colocation names are unique across the three kinds (they share the device-name and "
                    "pool-name space).",
                    node.get("id"),
                    kind,
                )
            )
    return findings


def _design(node: dict[str, Any]) -> dict[str, Any] | None:
    """The node's design as a dict, None when unset (an id-only design arrives as a bare string)."""
    design = node.get("design")
    return design if isinstance(design, dict) else None


def _design_name(design: dict[str, Any]) -> str:
    return str(design.get("name") or design.get("id") or "<unnamed design>")


def kit_without_design_findings(cleaned: dict[str, Any]) -> list[Finding]:
    """Rule 5: a zone or virtual colocation that declares kit has a design."""
    findings = []
    for kind in FABRIC_KINDS:
        for node in cleaned.get(kind) or []:
            entries = node.get("fabric_templates") or []
            if entries and not node.get("design"):
                findings.append(
                    Finding(
                        f"{kind} '{_name(node)}' declares {len(entries)} fabric_templates entries but has no design: "
                        "set its design (a TopologyTemplateColocationZone) so its pools are sized for the kit.",
                        node.get("id"),
                        kind,
                    )
                )
    return findings


def kit_within_design_findings(cleaned: dict[str, Any]) -> list[Finding]:
    """Rule 6: the quantity of each role stays within the design's max."""
    findings = []
    for kind in FABRIC_KINDS:
        for node in cleaned.get(kind) or []:
            design = _design(node)
            if design is None:
                continue
            totals: dict[str, int] = defaultdict(int)
            for entry in node.get("fabric_templates") or []:
                if entry.get("role") in ROLE_LIMIT:
                    totals[entry["role"]] += int(entry.get("quantity") or 0)
            for role, attribute in ROLE_LIMIT.items():
                limit = design.get(attribute)
                if limit is not None and totals[role] > int(limit):
                    findings.append(
                        Finding(
                            f"{kind} '{_name(node)}' declares {totals[role]} x {role} but its design "
                            f"'{_design_name(design)}' allows {attribute} = {limit}: lower the quantity or use a "
                            "larger design.",
                            node.get("id"),
                            kind,
                        )
                    )
    return findings


def zone_design_in_metro_findings(cleaned: dict[str, Any]) -> list[Finding]:
    """Rule 7: with a design on both the metro and the zone, the zone's design is one of the metro's zone_templates."""
    metros = {metro["id"]: metro for metro in cleaned.get(METRO) or [] if metro.get("id")}
    findings = []
    for kind in FABRIC_KINDS:
        for node in cleaned.get(kind) or []:
            design = _design(node)
            metro = metros.get((node.get("parent") or {}).get("id"))
            metro_design = _design(metro) if metro else None
            if design is None or metro is None or metro_design is None:
                continue
            templates = metro_design.get("zone_templates") or []
            if design.get("id") not in {template.get("id") for template in templates}:
                names = ", ".join(sorted(_name(template) for template in templates)) or "none"
                findings.append(
                    Finding(
                        f"{kind} '{_name(node)}' uses design '{_design_name(design)}', which metro '{_name(metro)}' "
                        f"does not allow: its design '{_design_name(metro_design)}' has zone_templates {names}.",
                        node.get("id"),
                        kind,
                    )
                )
    return findings


def metro_zone_count_findings(cleaned: dict[str, Any]) -> list[Finding]:
    """Rule 8: a metro with a design holds at most max_zones zones and virtual colocations together."""
    held: dict[str, list[str]] = defaultdict(list)
    for kind in FABRIC_KINDS:
        for node in cleaned.get(kind) or []:
            metro_id = (node.get("parent") or {}).get("id")
            if metro_id:
                held[metro_id].append(_name(node))
    findings = []
    for metro in cleaned.get(METRO) or []:
        design = _design(metro)
        names = sorted(held.get(metro.get("id"), []))
        if design is not None and design.get("max_zones") is not None and len(names) > int(design["max_zones"]):
            findings.append(
                Finding(
                    f"{METRO} '{_name(metro)}' holds {len(names)} zones and virtual colocations ({', '.join(names)}) "
                    f"but its design '{_design_name(design)}' allows max_zones = {design['max_zones']}: remove one "
                    "or use a larger metro design.",
                    metro.get("id"),
                    METRO,
                )
            )
    return findings


def _operator_name(node: dict[str, Any]) -> str:
    """The colocation operator above the virtual colocation's metro, empty when the query did not reach one."""
    operator = (node.get("parent") or {}).get("parent") or {}
    return str(operator.get("name") or "")


def _account_findings(node: dict[str, Any], account: dict[str, Any]) -> list[str]:
    """The problems of the billing account a terraform order is placed under, one text each."""
    problems = []
    if not str(account.get("account_id") or "").strip():
        problems.append(f"account '{_display_label(account)}' has no account_id (the billing account number)")
    provider = (account.get("provider") or {}).get("name")
    operator = _operator_name(node)
    if not provider:
        problems.append(f"account '{_display_label(account)}' has no provider")
    elif operator and str(provider).casefold() != operator.casefold():
        problems.append(
            f"account '{_display_label(account)}' is a '{provider}' account but the metro's operator is '{operator}'"
        )
    owner = account.get("owner") or {}
    emails = [email for email in owner.get("notification_emails") or [] if email]
    if not owner:
        problems.append(f"account '{_display_label(account)}' has no owner to notify")
    elif not emails:
        problems.append(f"account owner '{_display_label(owner)}' has no notification_emails")
    elif any("@" not in email for email in emails):
        problems.append(
            f"account owner '{_display_label(owner)}' has notification_emails {emails} that are not all e-mail addresses"
        )
    return problems


HA_MODES = ("active-passive",)
"""The HA domain modes the Terraform artifact can render: a cluster."""


def _ha_domains(appliance: dict[str, Any]) -> list[dict[str, Any]]:
    """The HA domains an appliance is a member of: its ``capabilities`` entries that carry a ``mode`` (the
    ManagedHA attribute), as opposed to e.g. the BGP overlay process."""
    return [
        capability
        for capability in appliance.get("capabilities") or []
        if isinstance(capability, dict) and "mode" in capability
    ]


def _appliance_problems(appliance: dict[str, Any], account_id: str | None) -> list[str]:
    """The problems of one appliance on its own: its account and the number of HA domains it is in."""
    label = f"appliance '{_name(appliance)}'"
    problems = []
    if account_id and _ref_id(appliance.get("account")) != account_id:
        problems.append(f"{label} is not ordered under the presence's account")
    domains = _ha_domains(appliance)
    if len(domains) > 1:
        names = ", ".join(sorted(f"'{_name(domain)}'" for domain in domains))
        problems.append(f"{label} is a member of {len(domains)} HA domains ({names}) but is in at most one")
    elif domains:
        domain = domains[0]
        if appliance.get("id") not in {member.get("id") for member in domain.get("capabilities") or []}:
            problems.append(f"{label} lists HA domain '{_name(domain)}' but is not one of its members")
    return problems


def _domain_problems(
    domain: dict[str, Any], reporter: dict[str, Any], appliances: dict[str, dict[str, Any]]
) -> list[str]:
    """The problems of one HA domain as a pair, named after ``reporter`` (the first member by name that lists it)."""
    label = f"appliance '{_name(reporter)}' (HA domain '{_name(domain)}')"
    members = domain.get("capabilities") or []
    problems = []
    if len(members) != 2:
        names = ", ".join(sorted(_name(member) for member in members)) or "none"
        problems.append(f"{label} has {len(members)} members ({names}) but an HA pair has exactly two")
    if domain.get("mode") not in HA_MODES:
        problems.append(
            f"{label} has mode '{domain.get('mode')}' but only {' and '.join(HA_MODES)} can be ordered (a cluster)"
        )
    for member in members:
        if member.get("id") == reporter.get("id"):
            continue
        partner = appliances.get(str(member.get("id")))
        if partner is None:
            problems.append(f"{label} has member '{_name(member)}', which is not an appliance of this presence")
        elif domain.get("id") not in {other.get("id") for other in _ha_domains(partner)}:
            problems.append(
                f"{label} has member '{_name(partner)}', which does not list the domain on its capabilities"
            )
    return problems


def _ha_problems(appliances: dict[str, dict[str, Any]]) -> list[str]:
    """The pair-level problems of every HA domain the presence's appliances list, each domain once."""
    listing: dict[str, list[dict[str, Any]]] = defaultdict(list)
    domains: dict[str, dict[str, Any]] = {}
    for appliance in appliances.values():
        found = _ha_domains(appliance)
        if len(found) == 1 and found[0].get("id"):
            domains[str(found[0]["id"])] = found[0]
            listing[str(found[0]["id"])].append(appliance)
    problems = []
    for domain_id, owners in listing.items():
        reporter = min(owners, key=_name)
        problems += _domain_problems(domains[domain_id], reporter, appliances)
    return problems


def terraform_order_data_findings(cleaned: dict[str, Any]) -> list[Finding]:
    """Rule 9: a virtual colocation routed to terraform has its account, owner e-mails and consistent appliances (HA domains)."""
    findings = []
    for node in cleaned.get(VIRTUAL_COLOCATION) or []:
        if node.get("infrastructure_orchestrator") != TERRAFORM:
            continue
        account = node.get("account")
        account = account if isinstance(account, dict) else {}
        problems = (
            _account_findings(node, account)
            if account
            else ["the presence has no account (the billing account of the order)"]
        )
        appliances = {
            str(device["id"]): device
            for device in node.get("devices") or []
            if isinstance(device, dict) and device.get("typename") == APPLIANCE and device.get("id")
        }
        for appliance in appliances.values():
            problems += _appliance_problems(appliance, _ref_id(account))
        problems += _ha_problems(appliances)
        findings.extend(
            Finding(
                f"{VIRTUAL_COLOCATION} '{_name(node)}' is routed to terraform but its order data is wrong: {problem}. "
                "The artifact prints the account, the owner's notification addresses and the appliances of the "
                "provider order.",
                node.get("id"),
                VIRTUAL_COLOCATION,
            )
            for problem in problems
        )
    return findings


RULES: tuple[Callable[[dict[str, Any]], list[Finding]], ...] = (
    template_kind_findings,
    serving_zone_metro_findings,
    serving_zone_firewall_findings,
    unique_name_findings,
    kit_without_design_findings,
    kit_within_design_findings,
    zone_design_in_metro_findings,
    metro_zone_count_findings,
    terraform_order_data_findings,
)
"""Every rule, run in order over the cleaned query result."""


class CheckColocationPlacement(InfrahubCheck):
    """Zones host physical kit, virtual colocations virtual kit; footprints are served by a firewall-holding zone of
    their metro; colocation names are unique; kit fits the design envelope of its zone and metro."""

    query = "colocation_placement"

    def validate(self, data: Any) -> None:
        cleaned = clean_data(data)
        for rule in RULES:
            for finding in rule(cleaned):
                self.log_error(message=finding.message, object_id=finding.object_id, object_type=finding.object_type)
