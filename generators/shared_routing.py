"""Shared routing objects mixin: the fabric-wide passwords, ASes and OSPF area that add_dc / the office fabric own."""

from __future__ import annotations

import asyncio
import secrets
from typing import TYPE_CHECKING, Any, NamedTuple

if TYPE_CHECKING:
    import logging

    from .pools import PoolMixin

from .helpers import RoutingStrategy
from .protocols import RoutingAutonomousSystem, RoutingOSPFArea, RoutingPassword


# Names/descriptions of the shared routing objects; they are the idempotency keys, spelled once here.
def _underlay_key_name(fabric_name: str) -> str:
    return f"{fabric_name}-underlay-key"


def _overlay_key_name(fabric_name: str) -> str:
    return f"{fabric_name}-overlay-key"


def _super_spine_as_description(fabric_name: str) -> str:
    return f"{fabric_name} super-spine underlay ASN"


def _lisp_site_key_name(fabric_name: str) -> str:
    return f"{fabric_name}-lisp-site-key"


def _overlay_as_description(fabric_name: str) -> str:
    return f"{fabric_name} overlay ASN for iBGP EVPN"


def _ospf_area_name(fabric_name: str) -> str:
    return f"{fabric_name}-ospf-area-0"


async def _none() -> None:
    """Stands in for a lookup the routing strategy does not need."""
    return None


class SharedRoutingIds(NamedTuple):
    """The ids ``_create_shared_routing_objects`` hands back to the DC generator."""

    super_spine_as_id: str | None
    overlay_as_id: str | None


class SharedRoutingObjectsMixin:
    """Mixin for the shared routing objects (auth keys, shared ASes, OSPF area); needs ``client``, ``logger``,
    ``fabric_name``, ``data``, ``resource_lock``, ``attach_refs``.
    """

    # Type-checker declarations, provided by CommonGenerator / InfrahubGenerator
    client: Any
    logger: logging.Logger
    fabric_name: str
    data: Any
    if TYPE_CHECKING:
        resource_lock = PoolMixin.resource_lock
    # Shared RoutingPassword ids this run has found, by name (_resolve_shared_passwords).
    _shared_password_ids: dict[str, str]

    async def _ensure_routing_password(self, *, name: str, description: str, track: bool = True) -> str | None:
        """Create or reuse a shared RoutingPassword by deterministic name, serialized per name.
        track=False: a run owning a key would delete it in cleanup and rotate the secret."""
        async with self.resource_lock(f"routing-password-{name}"):
            return await self._ensure_routing_password_locked(name=name, description=description, track=track)

    async def _ensure_routing_password_locked(self, *, name: str, description: str, track: bool) -> str | None:
        try:
            existing = await self.client.get(kind=RoutingPassword, name__value=name, raise_when_missing=False)
            if existing:
                if track:
                    self.client.group_context.related_node_ids.append(existing.id)
                return existing.id
        except Exception as exc:
            # Fatal: add_dc owns this key, and a run that could not re-track it would delete it in cleanup
            self.logger.error(f"Error querying RoutingPassword {name}: {exc}")
            return None

        try:
            obj = await self.client.create(
                kind=RoutingPassword,
                data={
                    "name": name,
                    "description": description,
                    "password": secrets.token_urlsafe(32),
                },
            )
            # A save claims the node for the run's group; an untracked key must opt out here.
            await obj.save(allow_upsert=True, **({} if track else {"update_group_context": False}))
            if track:
                self.client.group_context.related_node_ids.append(obj.id)
            self.logger.info(f"Created shared RoutingPassword: {name}")
            return obj.id
        except Exception as exc:
            self.logger.error(f"Could not create RoutingPassword {name}: {exc}")
            return None

    async def _create_shared_routing_objects(
        self,
        overlay_asn: int,
        asn_pool_id: str | None = None,
    ) -> SharedRoutingIds:
        """Create the shared DC-level routing state used by pod and rack generators; every failure is fatal.
        add_dc/dc_pod_cascade own and track it. Returns the super-spine underlay AS id (None without eBGP) and the
        fabric AS id (``evpn_rt_as``).
        """

        # Two keys, two lock names: they are created side by side.
        await asyncio.gather(
            self._ensure_routing_password(
                name=_underlay_key_name(self.fabric_name),
                description=f"Shared eBGP/OSPF underlay auth key for {self.fabric_name}",
            ),
            self._ensure_routing_password(
                name=_overlay_key_name(self.fabric_name),
                description=f"Shared BGP overlay/EVPN auth key for {self.fabric_name}",
            ),
        )

        strategy = self.data.get("routing_strategy", "ebgp-ebgp")

        # Super-spines share ONE underlay ASN (.dev/bgp.txt) whenever the underlay is eBGP; same find-or-create idiom as
        # the overlay AS.
        super_spine_as_id: str | None = None
        if strategy.split("-")[0] == "ebgp":
            try:
                super_spine_as_id = await self.ensure_shared_as(
                    description=_super_spine_as_description(self.fabric_name), asn_pool_id=asn_pool_id
                )
            except Exception as exc:
                self.logger.error(f"Failed to create shared super-spine AS: {exc}")

        # The fabric AS is created for every strategy: ebgp-ebgp devices don't use it as local-as, but EVPN
        # route-targets need one fabric-constant ASN. The description is the idempotency key; keep it unchanged.
        overlay_desc = _overlay_as_description(self.fabric_name)
        overlay_as_id: str | None = None
        try:
            overlay_as_id = await self._ensure_overlay_as(description=overlay_desc, overlay_asn=overlay_asn)
        except Exception as exc:
            self.logger.error(f"Failed to create shared overlay AS: {exc}")

        if strategy == RoutingStrategy.OSPF_IBGP.value:
            await self.ensure_ospf_area()

        return SharedRoutingIds(super_spine_as_id=super_spine_as_id, overlay_as_id=overlay_as_id)

    async def ensure_lisp_site_key(self) -> str | None:
        """Find or create the office's shared LISP site key; returns its id (None if not found or created)."""
        return await self._ensure_routing_password(
            name=_lisp_site_key_name(self.fabric_name),
            description=f"Shared LISP map-server site key for {self.fabric_name}",
        )

    async def ensure_ospf_area(self) -> str | None:
        """Find or create the fabric's OSPF backbone area (area 0), tracked in this run; returns its id or None."""
        area_name = _ospf_area_name(self.fabric_name)
        try:
            area_obj = await self.client.get(kind=RoutingOSPFArea, name__value=area_name, raise_when_missing=False)
            if area_obj:
                self.client.group_context.related_node_ids.append(area_obj.id)
            else:
                area_obj = await self.client.create(
                    kind=RoutingOSPFArea,
                    data={
                        "name": area_name,
                        "area": 0,
                        "area_type": "standard",
                        "description": f"OSPF backbone area for {self.fabric_name}",
                    },
                )
                await area_obj.save(allow_upsert=True)
                self.client.group_context.related_node_ids.append(area_obj.id)
                self.logger.info(f"Created shared OSPF area: {area_name}")
            return area_obj.id
        except Exception as exc:
            self.logger.error(f"Failed to create shared OSPF area: {exc}")
            return None

    async def _ensure_overlay_as(self, *, description: str, overlay_asn: int) -> str:
        """Find the overlay AS by its deterministic description (pinning ``overlay_asn``) or create it, tracked, under
        the ensure_shared_as lock.
        """
        async with self.resource_lock(f"shared-as-{description}"):
            existing = await self.client.filters(kind=RoutingAutonomousSystem, description__value=description)
            if existing:
                as_obj = existing[0]
                as_obj.asn.value = overlay_asn
                await as_obj.save(allow_upsert=True)
                self.logger.info(f"Updated shared overlay AS: AS{as_obj.asn.value} ({as_obj.id})")
            else:
                as_obj = await self.client.create(
                    kind=RoutingAutonomousSystem,
                    data={"asn": overlay_asn, "description": description},
                )
                await as_obj.save(allow_upsert=True)
                self.logger.info(f"Created shared overlay AS: AS{as_obj.asn.value} ({as_obj.id})")
            self.client.group_context.related_node_ids.append(as_obj.id)
            return as_obj.id

    async def ensure_shared_as(self, *, description: str, asn_pool_id: str | None, track: bool = True) -> str | None:
        """Find the AS with this deterministic description, or draw one from asn_pool_id, tracked unless track=False.
        The description is the idempotency key; serialized per description. Returns None without a match or pool."""
        async with self.resource_lock(f"shared-as-{description}"):
            return await self._ensure_shared_as_locked(description=description, asn_pool_id=asn_pool_id, track=track)

    async def _ensure_shared_as_locked(self, *, description: str, asn_pool_id: str | None, track: bool) -> str | None:
        existing = await self.client.filters(kind=RoutingAutonomousSystem, description__value=description)
        if existing:
            as_obj = existing[0]
        elif asn_pool_id:
            as_obj = await self.client.create(
                kind=RoutingAutonomousSystem,
                data={"asn": {"from_pool": {"id": asn_pool_id}}, "description": description},
            )
            # A save claims the node for the run's group; an AS no run may own must opt out, or its next run deletes it.
            await as_obj.save(allow_upsert=True, **({} if track else {"update_group_context": False}))
            self.logger.info(f"Created shared AS{as_obj.asn.value} '{description}' ({as_obj.id})")
        else:
            return None
        if track:
            self.client.group_context.related_node_ids.append(as_obj.id)
        return as_obj.id

    async def _resolve_shared_passwords(self) -> tuple[str | None, str | None]:
        """Find the shared underlay/overlay RoutingPassword IDs by name (one-shot); a missing password is non-fatal."""
        underlay_name, overlay_name = _underlay_key_name(self.fabric_name), _overlay_key_name(self.fabric_name)
        # Found ids only, per run; a missing one is looked up again on the next call
        found: dict[str, str] = getattr(self, "_shared_password_ids", {})
        missing = [name for name in (underlay_name, overlay_name) if name not in found]
        if missing:
            try:
                for password in await self.client.filters(kind=RoutingPassword, name__values=missing):
                    found[password.name.value] = password.id
            except Exception as e:
                self.logger.debug(f"Error querying RoutingPassword {missing}: {e}")
            self._shared_password_ids = found
        return found.get(underlay_name), found.get(overlay_name)

    async def _resolve_shared_objects(self, routing_strategy: str) -> tuple[str | None, str | None]:
        """Find the shared overlay AS and OSPF area; returns (overlay_as_id, ospf_area_id). One-shot, only referenced,
        never tracked.
        """
        wants_overlay_as = routing_strategy in (RoutingStrategy.EBGP_IBGP.value, RoutingStrategy.OSPF_IBGP.value)
        wants_ospf_area = routing_strategy == RoutingStrategy.OSPF_IBGP.value
        # Independent lookups: awaited together, not one after the other.
        overlay_as_id, ospf_area_id = await asyncio.gather(
            self._find_overlay_as_id() if wants_overlay_as else _none(),
            self._find_ospf_area_id() if wants_ospf_area else _none(),
        )
        return overlay_as_id, ospf_area_id

    async def _find_overlay_as_id(self) -> str | None:
        """The fabric's overlay AS id by its deterministic description, None when absent or unreadable."""
        overlay_desc = _overlay_as_description(self.fabric_name)
        try:
            existing = await self.client.filters(kind=RoutingAutonomousSystem, description__value=overlay_desc)
        except Exception as e:
            self.logger.warning(f"Error querying overlay AS for {self.fabric_name}: {e}")
            return None
        if not existing:
            return None
        self.logger.info(f"Found existing overlay AS: AS{existing[0].asn.value} ({existing[0].id})")
        return existing[0].id

    async def _find_ospf_area_id(self) -> str | None:
        """The fabric's OSPF area id by its deterministic name, None when absent or unreadable."""
        area_name = _ospf_area_name(self.fabric_name)
        try:
            area = await self.client.get(kind=RoutingOSPFArea, name__value=area_name)
        except Exception as e:
            self.logger.warning(f"Error querying OSPF area {area_name}: {e}")
            return None
        if not area:
            return None
        self.logger.info(f"Found existing OSPF area: {area_name}")
        return area.id
