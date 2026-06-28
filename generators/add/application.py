"""Generator: app-level automation triggered per AppApplication node.

Currently derives SecurityPolicyRule nodes from AppDependency edges:
  1. Look up (or create) a SecurityPolicy named after the application.
  2. Create one SecurityPolicyRule per AppDependency:
       - source_segment  = source_component.network_segment
       - destination_segment = target_component.network_segment
       - source_zone / destination_zone derived from each segment's security_zone
       - protocol / ports: taken from AppDependency if set, otherwise inferred
         from component_type pairs via PORT_MAP
       - action: permit
       - log: True for any cross-zone flow; False for same-zone flows
  3. Rules are named deterministically so upsert is idempotent.

When the source or destination segment typename == "CloudNetworkSegment", a
CloudSecurityGroupRule is created instead (attached to a CloudSecurityGroup that
is auto-created per application per VPC).

Engineers can review, edit, or disable individual rules in a Proposed Change
before merging. The generator will not re-create rules that already exist (same
policy + index uniqueness constraint guarantees this via allow_upsert=True).
"""

from __future__ import annotations

from typing import Any

from utils.data_cleaning import clean_data

from ..common import CommonGenerator


def _seg_cidr(seg: dict) -> str | None:
    """Extract a CIDR string from a segment dict (on-prem or cloud)."""
    # Cloud segment: cidr_block.prefix
    cidr_block = seg.get("cidr_block") or {}
    if cidr_block.get("prefix"):
        return cidr_block["prefix"]
    # On-prem segment: prefix is a list
    prefixes = seg.get("prefix") or []
    if prefixes:
        return prefixes[0].get("prefix")
    return None


def _resolve_port(
    dep: dict,
) -> tuple[str, int | None, int | None] | None:
    """Return (protocol, port_start, port_end) from an AppDependency node.

    Returns None when the dependency carries no port/protocol — the caller
    must skip rule creation and warn. Every dependency edge must carry
    explicit port information; there is no automatic fallback.
    """
    protocol = dep.get("protocol")
    port_start = dep.get("port_start")

    if protocol is None and port_start is None:
        return None

    return (protocol or "tcp"), port_start, dep.get("port_end")


# Starting rule index — leaves room below 100 for manually-crafted high-priority rules
RULE_INDEX_START = 100
RULE_INDEX_STEP = 10


class AppApplicationGenerator(CommonGenerator):
    """App-level generator: security policy derivation and future app-scoped automation.

    Triggered per AppApplication node (target group: app_applications).
    Query: application — returns the application with all components,
    their depends_on AppDependency edges (each carrying optional port/protocol),
    and each dependency's target component with its network_segment.
    """

    async def generate(self, data: dict[str, Any]) -> None:
        cleaned = clean_data(data)
        app_list = cleaned.get("AppApplication", [])
        if not app_list:
            self.logger.error("No AppApplication data in GraphQL response")
            return

        app = app_list[0]
        app_name: str = app.get("name", "")
        criticality: str = app.get("criticality", "medium")
        self.logger.info("Processing security rules for application: %s", app_name)

        components: list[dict] = app.get("children", [])
        if not components:
            self.logger.warning("Application %s has no components — nothing to do", app_name)
            return

        # ── Collect all dependency edges ──────────────────────────────────
        # Each edge is (src_component, dep_node, dst_component).
        # dep_node carries optional protocol/port_start/port_end.
        edges: list[tuple[dict, dict, dict]] = []
        for comp in components:
            for dep in comp.get("depends_on", []):
                target = dep.get("target") or {}
                if not target:
                    self.logger.warning(
                        "AppDependency '%s' has no target — skipping",
                        dep.get("name", dep.get("id", "?")),
                    )
                    continue
                edges.append((comp, dep, target))

        if not edges:
            self.logger.info("Application %s has no depends_on edges — no rules to generate", app_name)
            return

        self.logger.info("Found %d dependency edge(s) for %s", len(edges), app_name)

        # ── Ensure a SecurityPolicy exists for this application ───────────
        policy_name = f"app-{app_name}-dependencies"
        policy = await self._get_or_create_policy(policy_name, app_name)
        if policy is None:
            return
        policy_id = policy.id

        # ── Fetch existing rules for this policy to determine next index ──
        existing_rules = await self.client.filters(
            kind="SecurityPolicyRule",
            policy__ids=[policy_id],
        )
        used_indexes: set[int] = set()
        existing_names: set[str] = set()
        for r in existing_rules:
            await r.resolve()
            if getattr(r, "index", None) and r.index.value:
                used_indexes.add(int(r.index.value))
            if getattr(r, "name", None) and r.name.value:
                existing_names.add(r.name.value)

        next_index = RULE_INDEX_START
        if used_indexes:
            next_index = max(used_indexes) + RULE_INDEX_STEP

        # ── Generate one rule per dependency edge ─────────────────────────
        rules_created = 0
        rules_skipped = 0

        for src_comp, dep, dst_comp in edges:
            src_seg = src_comp.get("network_segment") or {}
            dst_seg = dst_comp.get("network_segment") or {}

            src_seg_id = src_seg.get("id")
            dst_seg_id = dst_seg.get("id")

            if not src_seg_id or not dst_seg_id:
                self.logger.warning(
                    "Dependency %s → %s: one or both components lack a network_segment — skipping",
                    src_comp.get("name", "?"),
                    dst_comp.get("name", "?"),
                )
                continue

            src_type = src_comp.get("component_type", "backend")
            dst_type = dst_comp.get("component_type", "backend")

            src_typename = src_seg.get("typename", "")
            dst_typename = dst_seg.get("typename", "")
            is_cloud = src_typename == "CloudNetworkSegment" or dst_typename == "CloudNetworkSegment"

            rule_name = self._rule_name(app_name, src_comp, dst_comp)

            if is_cloud:
                ok = await self._create_cloud_rule(app_name, src_comp, dst_comp, dep, rule_name, src_type, dst_type)
                if ok:
                    rules_created += 1
                else:
                    rules_skipped += 1
                continue

            port_info = _resolve_port(dep)
            if port_info is None:
                self.logger.warning(
                    "  Dependency '%s' (%s → %s) has no protocol/port — skipping rule creation",
                    dep.get("name", dep.get("id", "?")),
                    src_comp.get("name", "?"),
                    dst_comp.get("name", "?"),
                )
                rules_skipped += 1
                continue
            protocol, port_start, port_end = port_info

            src_zone = (src_seg.get("security_zone") or {}).get("name")
            dst_zone = (dst_seg.get("security_zone") or {}).get("name")
            cross_zone = src_zone != dst_zone

            if rule_name in existing_names:
                self.logger.info("  Rule '%s' already exists — registering with tracker", rule_name)
                for r in existing_rules:
                    if getattr(r, "name", None) and r.name.value == rule_name:
                        await r.save(allow_upsert=True)
                        break
                rules_skipped += 1
                continue

            rule_data: dict[str, Any] = {
                "policy": {"id": policy_id},
                "index": next_index,
                "name": rule_name,
                "action": "permit",
                "protocol": protocol,
                "log": cross_zone,
                "disabled": False,
                "description": (
                    dep.get("description")
                    or (
                        f"Auto-generated: {src_comp.get('name', '?')} ({src_type}) "
                        f"→ {dst_comp.get('name', '?')} ({dst_type})"
                    )
                ),
                "source_segment": {"id": src_seg_id},
                "destination_segment": {"id": dst_seg_id},
            }

            # Wire zones when available
            if src_zone:
                src_zone_obj = await self._get_zone(src_zone)
                if src_zone_obj:
                    rule_data["source_zone"] = {"id": src_zone_obj.id}

            if dst_zone:
                dst_zone_obj = await self._get_zone(dst_zone)
                if dst_zone_obj:
                    rule_data["destination_zone"] = {"id": dst_zone_obj.id}

            # Wire ports
            if port_start is not None:
                rule_data["port_start"] = port_start
            if port_end is not None:
                rule_data["port_end"] = port_end

            # Attach a security profile based on app criticality + cross-zone
            profile_name = self._pick_profile(criticality, cross_zone)
            if profile_name:
                profile = await self._get_profile(profile_name)
                if profile:
                    rule_data["security_profile"] = {"id": profile.id}

            src_isolation = src_seg.get("isolation_mode") or "normal"
            dst_isolation = dst_seg.get("isolation_mode") or "normal"
            apply_on_switch = src_isolation == "microsegmented" or dst_isolation == "microsegmented"
            rule_data["apply_on_switch"] = apply_on_switch

            try:
                rule = await self.client.create(kind="SecurityPolicyRule", data=rule_data)
                await rule.save(allow_upsert=True)
                self.logger.info(
                    "  Created rule [%d] '%s' (%s → %s, %s/%s)",
                    next_index,
                    rule_name,
                    src_seg.get("name", src_seg_id),
                    dst_seg.get("name", dst_seg_id),
                    protocol,
                    port_start or "any",
                )
                existing_names.add(rule_name)
                used_indexes.add(next_index)
                next_index += RULE_INDEX_STEP
                rules_created += 1
            except Exception as exc:
                self.logger.error("  Failed to create rule '%s': %s", rule_name, exc)

        # ── Attach policy to every involved segment ────────────────────────
        seg_edges = [(src, dst) for src, _dep, dst in edges]
        await self._attach_policy_to_segments(policy_id, seg_edges)

        self.logger.info(
            "Application %s: %d rule(s) created, %d already existed",
            app_name,
            rules_created,
            rules_skipped,
        )

    # ── Helpers ───────────────────────────────────────────────────────────

    async def _get_or_create_sg(self, sg_name: str, vnet_id: str, acct_id: str | None) -> Any | None:
        """Fetch or create a CloudSecurityGroup by name."""
        cache: dict[str, Any] = getattr(self, "_sg_cache", {})
        if sg_name in cache:
            return cache[sg_name]
        try:
            existing = await self.client.filters(kind="CloudSecurityGroup", name__value=sg_name)
            if existing:
                await existing[0].save(allow_upsert=True)
                cache[sg_name] = existing[0]
                self._sg_cache = cache
                return existing[0]
        except Exception:
            pass
        data: dict[str, Any] = {
            "name": sg_name,
            "virtual_network": {"id": vnet_id},
        }
        if acct_id:
            data["account"] = {"id": acct_id}
        try:
            sg = await self.client.create(kind="CloudSecurityGroup", data=data)
            await sg.save(allow_upsert=True)
            self.logger.info("Created CloudSecurityGroup: %s", sg_name)
            cache[sg_name] = sg
            self._sg_cache = cache
            return sg
        except Exception as exc:
            self.logger.error("Failed to create CloudSecurityGroup %s: %s", sg_name, exc)
            cache[sg_name] = None
            self._sg_cache = cache
            return None

    async def _create_cloud_rule(
        self,
        app_name: str,
        src_comp: dict,
        dst_comp: dict,
        dep: dict,
        rule_name: str,
        src_type: str,
        dst_type: str,
    ) -> bool:
        """Create a CloudSecurityGroupRule for a cloud segment dependency."""
        src_seg = src_comp.get("network_segment") or {}
        dst_seg = dst_comp.get("network_segment") or {}
        dst_typename = dst_seg.get("typename", "")

        # Prefer dst for ingress rules; fall back to src for egress
        if dst_typename == "CloudNetworkSegment":
            cloud_seg = dst_seg
            cloud_seg_is_dst = True
        else:
            cloud_seg = src_seg
            cloud_seg_is_dst = False

        vnet = cloud_seg.get("virtual_network") or {}
        vnet_id = vnet.get("id")
        acct = vnet.get("account") or {}
        acct_id = acct.get("id") if acct else None

        if not vnet_id:
            self.logger.warning(
                "Cloud rule %s: no virtual_network id on segment %s — skipping",
                rule_name,
                cloud_seg.get("name", "?"),
            )
            return False

        sg_name = f"sg-{app_name}"
        sg = await self._get_or_create_sg(sg_name, vnet_id, acct_id)
        if sg is None:
            return False

        port_info = _resolve_port(dep)
        if port_info is None:
            self.logger.warning(
                "  Cloud dependency '%s' (%s → %s) has no protocol/port — skipping rule creation",
                dep.get("name", dep.get("id", "?")),
                src_comp.get("name", "?"),
                dst_comp.get("name", "?"),
            )
            return False
        protocol, port_start, port_end = port_info
        direction = "ingress" if cloud_seg_is_dst else "egress"

        # Check if rule already exists
        try:
            existing = await self.client.filters(kind="CloudSecurityGroupRule", name__value=rule_name)
            if existing:
                await existing[0].save(allow_upsert=True)
                self.logger.info("  Cloud rule '%s' already exists", rule_name)
                return True
        except Exception:
            pass

        rule_data: dict[str, Any] = {
            "security_group": {"id": sg.id},
            "name": rule_name,
            "direction": direction,
            "protocol": protocol,
            "action": "allow",
            "log": True,
            "disabled": False,
            "description": (
                dep.get("description")
                or (
                    f"Auto-generated: {src_comp.get('name', '?')} ({src_type}) "
                    f"→ {dst_comp.get('name', '?')} ({dst_type})"
                )
            ),
        }
        if port_start is not None:
            rule_data["port_start"] = port_start
        if port_end is not None:
            rule_data["port_end"] = port_end

        # Source/dest CIDR for the non-cloud side
        if cloud_seg_is_dst:
            src_cidr = _seg_cidr(src_seg)
            if src_cidr:
                rule_data["source_cidr"] = src_cidr
        else:
            dst_cidr = _seg_cidr(dst_seg)
            if dst_cidr:
                rule_data["dest_cidr"] = dst_cidr

        try:
            rule = await self.client.create(kind="CloudSecurityGroupRule", data=rule_data)
            await rule.save(allow_upsert=True)
            self.logger.info(
                "  Created cloud rule '%s' (dir=%s, sg=%s, %s/%s)",
                rule_name,
                direction,
                sg_name,
                protocol,
                port_start or "any",
            )
            return True
        except Exception as exc:
            self.logger.error("  Failed to create cloud rule '%s': %s", rule_name, exc)
            return False

    async def _get_or_create_policy(self, policy_name: str, app_name: str) -> Any | None:
        """Fetch an existing SecurityPolicy or create a new one."""
        try:
            existing = await self.client.filters(
                kind="SecurityPolicy",
                name__value=policy_name,
            )
            if existing:
                self.logger.info("Using existing policy: %s", policy_name)
                await existing[0].save(allow_upsert=True)  # register with tracker
                return existing[0]
        except Exception as exc:
            self.logger.warning("Could not look up policy %s: %s", policy_name, exc)

        try:
            policy = await self.client.create(
                kind="SecurityPolicy",
                data={
                    "name": policy_name,
                    "description": f"Auto-generated dependency rules for application {app_name}",
                    "default_action": "deny",
                    "enabled": True,
                },
            )
            await policy.save(allow_upsert=True)
            self.logger.info("Created policy: %s", policy_name)
            return policy
        except Exception as exc:
            self.logger.error("Failed to create policy %s: %s", policy_name, exc)
            return None

    async def _get_zone(self, zone_name: str) -> Any | None:
        """Fetch a SecurityZone by name (cached per generator run)."""
        cache: dict[str, Any] = getattr(self, "_zone_cache", {})
        if zone_name not in cache:
            try:
                zones = await self.client.filters(
                    kind="SecurityZone",
                    name__value=zone_name,
                )
                cache[zone_name] = zones[0] if zones else None
            except Exception:
                cache[zone_name] = None
            self._zone_cache = cache
        return cache[zone_name]

    async def _get_profile(self, profile_name: str) -> Any | None:
        """Fetch a SecuritySecurityProfile by name (cached)."""
        cache: dict[str, Any] = getattr(self, "_profile_cache", {})
        if profile_name not in cache:
            try:
                profiles = await self.client.filters(
                    kind="SecuritySecurityProfile",
                    name__value=profile_name,
                )
                cache[profile_name] = profiles[0] if profiles else None
            except Exception:
                cache[profile_name] = None
            self._profile_cache = cache
        return cache[profile_name]

    async def _attach_policy_to_segments(
        self,
        policy_id: str,
        edges: list[tuple[dict, dict]],
    ) -> None:
        """Ensure every segment involved in this application's rules has the policy
        in its security_policies relationship."""
        seen_seg_ids: set[str] = set()
        for src_comp, dst_comp in edges:
            for comp in (src_comp, dst_comp):
                seg = comp.get("network_segment") or {}
                seg_id = seg.get("id")
                if not seg_id or seg_id in seen_seg_ids:
                    continue
                seen_seg_ids.add(seg_id)

                seg_typename = seg.get("typename", "ManagedVxlanSegment")
                try:
                    seg_obj = await self.client.get(kind=seg_typename, id=seg_id)
                    policies_rel = getattr(seg_obj, "security_policies")
                    await policies_rel.fetch()
                    existing_policy_ids = {peer.id for peer in policies_rel.peers}
                    if policy_id not in existing_policy_ids:
                        await policies_rel.add({"id": policy_id})
                        await seg_obj.save(allow_upsert=True)
                        self.logger.info(
                            "  Attached policy to segment %s",
                            seg.get("name", seg_id),
                        )
                    else:
                        await seg_obj.save(allow_upsert=True)  # register with tracker
                except Exception as exc:
                    self.logger.warning(
                        "  Could not attach policy to segment %s: %s",
                        seg.get("name", seg_id),
                        exc,
                    )

    @staticmethod
    def _rule_name(app_name: str, src: dict, dst: dict) -> str:
        """Build a deterministic, human-readable rule name."""
        src_slug = src.get("slug") or src.get("name", "src")
        dst_slug = dst.get("slug") or dst.get("name", "dst")
        src_short = src_slug.split("-")[-1] if "-" in src_slug else src_slug
        dst_short = dst_slug.split("-")[-1] if "-" in dst_slug else dst_slug
        return f"{app_name}-{src_short}-to-{dst_short}"

    @staticmethod
    def _pick_profile(criticality: str, cross_zone: bool) -> str | None:
        """Select a threat-prevention profile based on app criticality and flow type."""
        if not cross_zone:
            return None  # same-zone east-west — no profile needed
        mapping = {
            "critical": "strict",
            "high": "strict",
            "medium": "standard",
            "low": "lightweight",
        }
        return mapping.get(criticality, "standard")
