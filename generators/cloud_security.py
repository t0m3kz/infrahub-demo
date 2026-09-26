"""Mixin for cloud-side (CloudSecurityGroup/Rule) dependency rules."""

from __future__ import annotations

from typing import TYPE_CHECKING, Any

from .helpers.rules import RulesPlanner
from .named_objects import GetOrCreateByNameMixin
from .protocols import CloudSecurityGroup, CloudSecurityGroupRule

if TYPE_CHECKING:
    import logging


class CloudSecurityRuleMixin(GetOrCreateByNameMixin):
    """Cloud-side dependency rules — dispatched from _reconcile_application_rules
    via RulesPlanner.is_cloud_dependency() whenever either side of an
    AppDependency is a CloudNetworkSegment, instead of the on-prem
    SecurityPolicy/SecurityPolicyRule path (SegmentFirewallMixin), which can't
    reference a cloud segment's virtual_network/account.

    Expects the host class to provide: ``client``, ``logger``.
    """

    client: Any
    logger: logging.Logger

    async def _get_or_create_sg(self, sg_name: str, vnet_id: str, acct_id: str | None) -> Any | None:
        """Get-or-create the CloudSecurityGroup an app's cloud-side dependency
        rules attach to. Cached per-run since multiple dependencies for the
        same app typically share one SG."""
        cache: dict[str, Any] = getattr(self, "_sg_cache", {})
        if sg_name in cache:
            return cache[sg_name]

        data: dict[str, Any] = {"name": sg_name, "virtual_network": {"id": vnet_id}}
        if acct_id:
            data["account"] = {"id": acct_id}
        sg = await self._get_or_create_by_name(
            kind=CloudSecurityGroup,
            name=sg_name,
            create_data=data,
            created_log="Created CloudSecurityGroup: %s",
        )
        cache[sg_name] = sg
        self._sg_cache = cache
        return sg

    async def _create_cloud_rule(
        self,
        app_name: str,
        src_comp: dict,
        dst_comp: dict,
        dep: dict,
        rule_name: str,
    ) -> bool:
        """Create a CloudSecurityGroupRule for a dependency where either side
        is a CloudNetworkSegment."""
        planner = RulesPlanner()
        src_seg = src_comp.get("network_segment") or {}
        dst_seg = dst_comp.get("network_segment") or {}
        dst_typename = dst_seg.get("typename", "")

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
                "Cloud rule %s: no virtual_network id on segment %s - skipping",
                rule_name,
                cloud_seg.get("name", "?"),
            )
            return False

        sg_name = f"sg-{app_name}"
        sg = await self._get_or_create_sg(sg_name, vnet_id, acct_id)
        if sg is None:
            return False

        port_info = RulesPlanner.resolve_port(dep)
        if port_info is None:
            self.logger.warning(
                "  Cloud dependency '%s' (%s -> %s) has no protocol/port - skipping rule creation",
                dep.get("name", dep.get("id", "?")),
                src_comp.get("name", "?"),
                dst_comp.get("name", "?"),
            )
            return False
        protocol, port_start, port_end = port_info
        direction = "ingress" if cloud_seg_is_dst else "egress"

        try:
            existing = await self.client.filters(kind=CloudSecurityGroupRule, name__value=rule_name)
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
            "description": planner.rule_description(dep, src_comp, dst_comp),
        }
        if port_start is not None:
            rule_data["port_start"] = port_start
        if port_end is not None:
            rule_data["port_end"] = port_end

        if cloud_seg_is_dst:
            src_cidr = planner.seg_cidr(src_seg)
            if src_cidr:
                rule_data["source_cidr"] = src_cidr
        else:
            dst_cidr = planner.seg_cidr(dst_seg)
            if dst_cidr:
                rule_data["dest_cidr"] = dst_cidr

        try:
            rule = await self.client.create(kind=CloudSecurityGroupRule, data=rule_data)
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
