"""Mixin for ZTNA/proxy-policy dependency rules (external egress + private-access publishing)."""

from __future__ import annotations

from typing import TYPE_CHECKING, Any, Callable

from utils.dependency_access import dependency_access_status
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
        if not proxy_service.get("id"):
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

        # A proxy rule matches a host on a list of ports, so one dependency
        # stays one rule however many ports it opens.
        return await self._upsert_owner_proxy_rule(
            owner=owner,
            service=proxy_service,
            purpose="egress",
            policies=proxy_policies,
            subject=f"Dependency '{dep_ref}' source '{src_label}'",
            rule_data={
                "name": planner.rule_name(app_name, src_comp, {}, dep),
                "action": "allow",
                "destination_type": "fqdn",
                "destination": external_fqdn,
                "ports": [PortProfileHelper.format_port_spec(port) for port in ports],
                "description": planner.rule_description(dep, src_comp, {}),
            },
        )

    async def _upsert_owner_proxy_rule(
        self,
        *,
        owner: dict[str, Any],
        service: dict[str, Any],
        purpose: str,
        policies: dict[str, Any],
        subject: str,
        rule_data: dict[str, Any],
    ) -> bool:
        """Upsert ``rule_data`` into the owner's ``purpose`` ProxyPolicy on ``service``.

        The policy is fetched or created (and attached to the owner) once per
        owner/service, then cached in ``policies``. An existing rule of the
        same name is updated in place. False when the owner has no id, the
        policy cannot be made, or the save fails.
        """
        owner_org_id = str(owner.get("org_id") or owner.get("id") or "")
        owner_node_id = str(owner.get("id") or "")
        if not owner_org_id or not owner_node_id:
            self.logger.warning("  %s has no resolvable owner - skipping", subject)
            return False

        service_id = service["id"]
        policy_key = f"{owner_org_id}:{service_id}"
        policy = policies.get(policy_key)
        if policy is None:
            service_name = str(service.get("name") or service_id)
            policy = await self._get_or_create_proxy_policy(f"proxy-{owner_org_id}-{service_name}-{purpose}")
            if policy is None:
                return False
            policies[policy_key] = policy
            await self._attach_proxy_policy_to_owner(owner_id=owner_node_id, policy_id=policy.id)

        rule_name = rule_data["name"]
        data: dict[str, Any] = {"policy": {"id": policy.id}, **rule_data}
        existing_rule = await self._find_rule_by_name(ProxyPolicyRule, policy.id, rule_name)
        if existing_rule is not None:
            data["id"] = existing_rule.id
        try:
            rule = await self.client.create(kind=ProxyPolicyRule, data=data)
            await rule.save(allow_upsert=True)
        except Exception as exc:
            self.logger.error("  Failed to reconcile ProxyPolicyRule '%s': %s", rule_name, exc)
            return False
        self.logger.info("  Reconciled ProxyPolicyRule '%s' (-> %s %s)", rule_name, data["destination"], data["ports"])
        return True

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

    async def _attach_proxy_policy_to_owner(self, owner_id: str, policy_id: str) -> None:
        try:
            owner_obj = await self.client.get(kind="OrganizationCustomer", id=owner_id, include=["proxy_policies"])
            policies_rel = getattr(owner_obj, "proxy_policies")
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
            if dep.get("source_profile") and dependency_access_status(dep) != "denied"
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
            if not broker.get("id"):
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

            profiles = sorted({str((dep.get("source_profile") or {}).get("name") or "?") for dep in grants})
            published = await self._upsert_owner_proxy_rule(
                owner=owner,
                service=broker,
                purpose="private-access",
                policies=policies,
                subject=f"Component '{comp_label}'",
                rule_data={
                    "name": f"publish-{component_fqdn}",
                    "action": "allow",
                    "destination_type": "fqdn",
                    "destination": component_fqdn,
                    "ports": ports,
                    "description": f"Publish {app_name}/{component.get('name') or comp_label} via private access"
                    f" broker for {', '.join(profiles)}",
                },
            )
            if published:
                created += 1
            else:
                skipped += 1

        return created, skipped
