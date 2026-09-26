"""Mixin for ZTNA/proxy-policy dependency rules (external + private-access endpoints)."""

from __future__ import annotations

from typing import TYPE_CHECKING, Any, Callable

from .helpers.rules import RulesPlanner
from .named_objects import GetOrCreateByNameMixin
from .protocols import ProxyPolicy, ProxyPolicyRule

if TYPE_CHECKING:
    import logging


class ZtnaMixin(GetOrCreateByNameMixin):
    """Owner-scoped ProxyPolicy/ProxyPolicyRule handling: external_service
    dependencies (egress through the owner's egress_service) and
    private_access endpoints (published through the owner's
    private_access_service, e.g. Zscaler ZPA — plus deriving a
    SecurityAccessProfile/SecurityIdentityGroup for them when the author
    hasn't set one explicitly).

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
        dst_endpoint: dict[str, Any],
        proxy_policies: dict[str, Any],
    ) -> bool:
        """Turn an external endpoint dependency into an owner-scoped ProxyPolicyRule."""
        planner = RulesPlanner()
        dep_ref = dep.get("name", dep.get("id", "?"))
        src_label = src_comp.get("slug") or src_comp.get("name") or "?"

        owner = (src_comp.get("parent") or {}).get("owner") or {}
        proxy_service = owner.get("egress_service") or {}
        proxy_id = proxy_service.get("id")
        if not proxy_id:
            self.logger.warning(
                "  Dependency '%s' targets an external endpoint but source owner for '%s' has no egress_service - skipping",
                dep_ref,
                src_label,
            )
            return False

        external_fqdn = str(dst_endpoint.get("fqdn") or "").strip()
        if not external_fqdn:
            self.logger.warning(
                "  Dependency '%s': external endpoint '%s' has no fqdn - skipping",
                dep_ref,
                dst_endpoint.get("name") or "?",
            )
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

        dst_comp = dst_endpoint.get("parent") or {}
        rule_name = planner.rule_name(app_name, src_comp, dst_comp)
        rule_data: dict[str, Any] = {
            "policy": {"id": policy.id},
            "name": rule_name,
            "action": "allow",
            "destination_type": "fqdn",
            "destination": external_fqdn,
            "description": planner.rule_description(dep, src_comp, dst_comp),
        }

        existing_rule = await self._find_existing_proxy_policy_rule(policy_id=policy.id, rule_name=rule_name)
        try:
            if existing_rule is not None:
                rule_data["id"] = existing_rule.id
            rule = await self.client.create(kind=ProxyPolicyRule, data=rule_data)
            await rule.save(allow_upsert=True)
            self.logger.info("  Reconciled ProxyPolicyRule '%s' (-> %s)", rule_name, external_fqdn)
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

    async def _reconcile_private_access_endpoints(
        self,
        app_name: str,
        components: list[dict[str, Any]],
        app_security_profile: str = "internal_standard",
    ) -> tuple[int, int]:
        """Publish every private_access endpoint via its owning customer's
        private_access_service (ZTNA broker: e.g. Zscaler ZPA), and derive its
        SecurityAccessProfile/SecurityIdentityGroup when the author hasn't set
        one explicitly.

        Not dependency-edge-driven like external_service/internal_service:
        who may reach a private_access endpoint is gated by its own
        access_profile (MFA/device posture/allowed_groups), not by which
        component "depends on" it, so every such endpoint gets published
        here regardless of whether any AppDependency targets it — matching
        this module's own docstring ("Device/proxy selections are resolved
        from the owning customer... rather than authored on every
        AppComponent").
        """
        created = 0
        skipped = 0
        policies: dict[str, Any] = {}

        for component in components:
            owner = (component.get("parent") or {}).get("owner") or {}
            for endpoint in component.get("children") or []:
                if endpoint.get("endpoint_type") != "private_access":
                    continue

                endpoint_name = str(endpoint.get("name") or endpoint.get("id") or "?")
                comp_label = component.get("slug") or component.get("name") or "?"

                owner_org_id_raw = str(owner.get("org_id") or owner.get("id") or "")
                if owner_org_id_raw and not endpoint.get("access_profile"):
                    await self._ensure_endpoint_access_profile(
                        endpoint_id=str(endpoint.get("id") or ""),
                        endpoint_name=endpoint_name,
                        owner_org_id=owner_org_id_raw,
                        app_security_profile=app_security_profile,
                    )

                broker = owner.get("private_access_service") or {}
                broker_id = broker.get("id")
                if not broker_id:
                    self.logger.warning(
                        "  Endpoint '%s' on '%s' is private_access but its owner has no"
                        " private_access_service - skipping publish",
                        endpoint_name,
                        comp_label,
                    )
                    skipped += 1
                    continue

                endpoint_fqdn = str(endpoint.get("fqdn") or "").strip()
                if not endpoint_fqdn:
                    self.logger.warning(
                        "  Endpoint '%s' on '%s' is private_access but has no fqdn - skipping publish",
                        endpoint_name,
                        comp_label,
                    )
                    skipped += 1
                    continue

                owner_org_id = str(owner.get("org_id") or owner.get("id") or "")
                owner_node_id = str(owner.get("id") or "")
                if not owner_org_id or not owner_node_id:
                    self.logger.warning(
                        "  Endpoint '%s' on '%s' has no resolvable owner - skipping publish",
                        endpoint_name,
                        comp_label,
                    )
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

                rule_name = f"publish-{comp_label}-{endpoint_name}"
                rule_data: dict[str, Any] = {
                    "policy": {"id": policy.id},
                    "name": rule_name,
                    "action": "allow",
                    "destination_type": "fqdn",
                    "destination": endpoint_fqdn,
                    "description": f"Publish {comp_label}/{endpoint_name} via private access broker",
                }

                existing_rule = await self._find_existing_proxy_policy_rule(policy_id=policy.id, rule_name=rule_name)
                try:
                    if existing_rule is not None:
                        rule_data["id"] = existing_rule.id
                    rule = await self.client.create(kind=ProxyPolicyRule, data=rule_data)
                    await rule.save(allow_upsert=True)
                    self.logger.info("  Published private-access endpoint '%s' (-> %s)", rule_name, endpoint_fqdn)
                    created += 1
                except Exception as exc:
                    self.logger.error("  Failed to publish private-access endpoint '%s': %s", rule_name, exc)
                    skipped += 1

        return created, skipped

    async def _ensure_endpoint_access_profile(
        self,
        *,
        endpoint_id: str,
        endpoint_name: str,
        owner_org_id: str,
        app_security_profile: str,
    ) -> None:
        """Derive and attach a SecurityAccessProfile/SecurityIdentityGroup for
        a private_access endpoint that has none, so ZTNA access policy
        doesn't have to be hand-authored per customer. Skipped entirely when
        the endpoint already has an explicit access_profile — never clobbers
        an author's override.
        """
        if not endpoint_id:
            return

        org_slug = owner_org_id.lower()
        group_name = f"{org_slug}-engineering"
        profile_name = f"{org_slug}-private-access-standard"

        group = await self._get_or_create_by_name(
            kind="SecurityIdentityGroup",
            name=group_name,
            create_data={
                "name": group_name,
                "description": f"{owner_org_id} engineering users",
                "source": "manual",
            },
            found_log="Using existing identity group: %s",
            created_log="Created identity group: %s",
        )
        if group is None:
            return

        policy = RulesPlanner.pick_access_policy(app_security_profile)
        profile = await self._get_or_create_by_name(
            kind="SecurityAccessProfile",
            name=profile_name,
            create_data={
                "name": profile_name,
                "description": f"{owner_org_id} standard private-access controls",
                "allowed_groups": [group.id],
                **policy,
            },
            found_log="Using existing access profile: %s",
            created_log="Created access profile: %s",
        )
        if profile is None:
            return

        try:
            endpoint = await self.client.create(
                kind="AppEndpoint",
                data={"id": endpoint_id, "access_profile": {"id": profile.id}},
            )
            await endpoint.save(allow_upsert=True)
            self.logger.info("  Derived access_profile '%s' for endpoint '%s'", profile_name, endpoint_name)
        except Exception as exc:
            self.logger.warning("  Failed to set access_profile on endpoint '%s': %s", endpoint_name, exc)
