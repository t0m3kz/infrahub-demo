"""Mixin for ZTNA/proxy-policy dependency rules (external egress + private-access publishing)."""

from __future__ import annotations

from typing import TYPE_CHECKING, Any, Callable

from utils.ports import PortProfileHelper

from .helpers.rules import RulesPlanner
from .named_objects import GetOrCreateByNameMixin
from .protocols import ProxyPolicy, ProxyPolicyRule

if TYPE_CHECKING:
    import logging


class ZtnaMixin(GetOrCreateByNameMixin):
    """Owner-scoped ProxyPolicy/ProxyPolicyRule handling: target_fqdn
    dependencies (egress through the owner's egress_service) and components
    granted to an access profile (published through the owner's
    private_access_service, e.g. Zscaler ZPA).

    Expects the host class to provide: ``client``, ``logger``, ``_safe_rel_add``.
    """

    client: Any
    logger: logging.Logger
    _safe_rel_add: Callable[..., Any]

    async def _reconcile_proxy_rule(
        self,
        app_name: str,
        src_comp: dict[str, Any],
        dep: dict[str, Any],
        proxy_policies: dict[str, Any],
    ) -> bool:
        """Turn a target_fqdn dependency into an owner-scoped egress ProxyPolicyRule."""
        planner = RulesPlanner()
        dep_ref = dep.get("name", dep.get("id", "?"))
        src_label = src_comp.get("fqdn") or src_comp.get("name") or "?"

        # checks/app_dependency.py reports both of these as errors; the
        # generator only refuses to write a rule it cannot place.
        owner = (src_comp.get("parent") or {}).get("owner") or {}
        proxy_service = owner.get("egress_service") or {}
        proxy_id = proxy_service.get("id")
        if not proxy_id:
            self.logger.warning(
                "  Dependency '%s' targets an external fqdn but source owner for '%s' has no egress_service - skipping",
                dep_ref,
                src_label,
            )
            return False

        external_fqdn = str(dep.get("target_fqdn") or "").strip()
        if not external_fqdn:
            self.logger.warning("  Dependency '%s' has a blank target_fqdn - skipping", dep_ref)
            return False
        try:
            ports = planner.resolve_ports(dep)
        except ValueError as exc:
            self.logger.warning("  Dependency '%s': %s - skipping", dep_ref, exc)
            return False
        if not ports:
            self.logger.warning("  Dependency '%s' targets %s without ports - skipping", dep_ref, external_fqdn)
            return False

        owner_org_id = str(owner.get("org_id") or owner.get("id") or "")
        owner_node_id = str(owner.get("id") or "")
        if not owner_org_id or not owner_node_id:
            self.logger.warning("  Dependency '%s' source '%s' has no owner identifier - skipping", dep_ref, src_label)
            return False
        policy_key = f"{owner_org_id}:{proxy_id}"
        policy = proxy_policies.get(policy_key)
        if policy is None:
            proxy_name = str(proxy_service.get("name") or proxy_id)
            policy_name = f"proxy-{owner_org_id}-{proxy_name}-egress"
            policy = await self._get_or_create_proxy_policy(policy_name)
            if policy is None:
                return False
            proxy_policies[policy_key] = policy

        await self._attach_proxy_policy_to_owner(owner_id=owner_node_id, policy_id=policy.id)

        # A proxy rule matches a host on a list of ports, so one dependency
        # stays one rule however many ports it opens.
        rule_name = planner.rule_name(app_name, src_comp, {}, dep)
        rule_data: dict[str, Any] = {
            "policy": {"id": policy.id},
            "name": rule_name,
            "action": "allow",
            "destination_type": "fqdn",
            "destination": external_fqdn,
            "ports": [PortProfileHelper.format_port_spec(port) for port in ports],
            "description": planner.rule_description(dep, src_comp, {}),
        }

        existing_rule = await self._find_existing_proxy_policy_rule(policy_id=policy.id, rule_name=rule_name)
        try:
            if existing_rule is not None:
                rule_data["id"] = existing_rule.id
            rule = await self.client.create(kind=ProxyPolicyRule, data=rule_data)
            await rule.save(allow_upsert=True)
            self.logger.info(
                "  Reconciled ProxyPolicyRule '%s' (-> %s %s)", rule_name, external_fqdn, rule_data["ports"]
            )
            return True
        except Exception as exc:
            self.logger.error("  Failed to reconcile ProxyPolicyRule '%s': %s", rule_name, exc)
            return False

    async def _get_or_create_proxy_policy(self, policy_name: str) -> Any | None:
        description_subject = policy_name.removesuffix("-egress").removesuffix("-private-access")
        return await self._get_or_create_by_name(
            kind=ProxyPolicy,
            name=policy_name,
            create_data={
                "name": policy_name,
                "description": f"Auto-generated egress rules for {description_subject}",
                "policy_type": "customer",
                "default_action": "block",
                "enabled": True,
            },
            created_log="Created proxy policy: %s",
        )

    async def _find_existing_proxy_policy_rule(self, policy_id: str, rule_name: str) -> Any | None:
        existing_rules = await self.client.filters(kind=ProxyPolicyRule, policy__ids=[policy_id])
        for rule in existing_rules:
            if getattr(rule, "name", None) and rule.name.value == rule_name:
                return rule
        return None

    async def _attach_proxy_policy_to_owner(self, owner_id: str, policy_id: str) -> None:
        try:
            owner_obj = await self.client.get(kind="OrganizationCustomer", id=owner_id)
            policies_rel = getattr(owner_obj, "proxy_policies")
            await policies_rel.fetch()
            if policy_id not in {peer.id for peer in policies_rel.peers}:
                await self._safe_rel_add(policies_rel, {"id": policy_id})
                await owner_obj.save(allow_upsert=True, update_group_context=False)
        except Exception as exc:
            self.logger.warning("  Could not attach proxy policy to owner %s: %s", owner_id, exc)

    @staticmethod
    def private_access_grants(component: dict[str, Any]) -> list[dict[str, Any]]:
        """Dependencies that admit an access profile's users to this component.

        A denied grant admits nobody. A component-sourced dependency is an
        ordinary call, not a ZTNA grant.
        """
        return [
            dep
            for dep in component.get("dependents") or []
            if dep.get("source_profile") and RulesPlanner.dependency_access_status(dep) != "denied"
        ]

    async def _reconcile_private_access_components(
        self,
        app_name: str,
        components: list[dict[str, Any]],
    ) -> tuple[int, int]:
        """Publish every component an access profile is granted to, via its
        owning customer's private_access_service (ZTNA broker: e.g. Zscaler ZPA).

        The grant is an AppDependency whose source_profile is a
        SecurityAccessProfile: it says which users (allowed_groups,
        MFA/device posture) reach the component, and on which of its ports.
        A component nobody is granted to stays unpublished.
        """
        created = 0
        skipped = 0
        policies: dict[str, Any] = {}

        for component in components:
            grants = self.private_access_grants(component)
            if not grants:
                continue

            owner = (component.get("parent") or {}).get("owner") or {}
            comp_label = str(component.get("fqdn") or component.get("name") or component.get("id") or "?")

            broker = owner.get("private_access_service") or {}
            broker_id = broker.get("id")
            if not broker_id:
                self.logger.warning(
                    "  Component '%s' is granted to an access profile but its owner has no"
                    " private_access_service - skipping publish",
                    comp_label,
                )
                skipped += 1
                continue

            component_fqdn = str(component.get("fqdn") or "").strip()
            if not component_fqdn:
                self.logger.warning("  Component '%s' has no fqdn - skipping publish", comp_label)
                skipped += 1
                continue

            ports: list[str] = []
            try:
                for dep in grants:
                    for port in RulesPlanner.resolve_ports(dep, component):
                        spec = PortProfileHelper.format_port_spec(port)
                        if spec not in ports:
                            ports.append(spec)
            except ValueError as exc:
                self.logger.warning("  Component '%s': %s - skipping publish", comp_label, exc)
                skipped += 1
                continue
            if not ports:
                # An empty port list would publish every port.
                self.logger.warning("  Component '%s' is granted without ports - skipping publish", comp_label)
                skipped += 1
                continue

            owner_org_id = str(owner.get("org_id") or owner.get("id") or "")
            owner_node_id = str(owner.get("id") or "")
            if not owner_org_id or not owner_node_id:
                self.logger.warning("  Component '%s' has no resolvable owner - skipping publish", comp_label)
                skipped += 1
                continue

            policy_key = f"{owner_org_id}:{broker_id}"
            policy = policies.get(policy_key)
            if policy is None:
                broker_name = str(broker.get("name") or broker_id)
                policy_name = f"proxy-{owner_org_id}-{broker_name}-private-access"
                policy = await self._get_or_create_proxy_policy(policy_name)
                if policy is None:
                    skipped += 1
                    continue
                policies[policy_key] = policy

            await self._attach_proxy_policy_to_owner(owner_id=owner_node_id, policy_id=policy.id)

            profiles = sorted({str((dep.get("source_profile") or {}).get("name") or "?") for dep in grants})
            rule_name = f"publish-{component_fqdn}"
            rule_data: dict[str, Any] = {
                "policy": {"id": policy.id},
                "name": rule_name,
                "action": "allow",
                "destination_type": "fqdn",
                "destination": component_fqdn,
                "ports": ports,
                "description": f"Publish {app_name}/{component.get('name') or comp_label} via private access broker"
                f" for {', '.join(profiles)}",
            }

            existing_rule = await self._find_existing_proxy_policy_rule(policy_id=policy.id, rule_name=rule_name)
            try:
                if existing_rule is not None:
                    rule_data["id"] = existing_rule.id
                rule = await self.client.create(kind=ProxyPolicyRule, data=rule_data)
                await rule.save(allow_upsert=True)
                self.logger.info("  Published private-access component '%s' (-> %s)", rule_name, component_fqdn)
                created += 1
            except Exception as exc:
                self.logger.error("  Failed to publish private-access component '%s': %s", rule_name, exc)
                skipped += 1

        return created, skipped
