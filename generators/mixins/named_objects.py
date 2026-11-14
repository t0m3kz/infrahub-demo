"""Generic mixin for get-or-create-by-name and rule-by-name lookups."""

from __future__ import annotations

from typing import TYPE_CHECKING, Any

from generators.mixins.connections import tracked_save_kwargs

if TYPE_CHECKING:
    import logging


class GetOrCreateByNameMixin:
    """Get-or-create-by-name idiom for catalog objects; needs ``client`` and ``logger`` on the host class."""

    client: Any
    logger: logging.Logger

    async def _get_or_create_by_name(
        self,
        *,
        kind: Any,
        name: str,
        create_data: dict[str, Any],
        found_log: str | None = None,
        created_log: str | None = None,
        track: bool = True,
    ) -> Any | None:
        """Filter by name__value and return the upserted object if found, else create and save it.
        ``track`` True: the run's group owns the object and a later run deletes it if dropped.
        False: a shared catalog object, saved with update_group_context=False so no run claims it."""
        try:
            existing = await self.client.filters(kind=kind, name__value=name)
            if existing:
                if found_log:
                    self.logger.info(found_log, name)
                if track:
                    await existing[0].save(allow_upsert=True)
                return existing[0]
        except Exception as exc:
            self.logger.warning("Could not look up %s: %s", name, exc)

        try:
            obj = await self.client.create(kind=kind, data=create_data)
            await obj.save(**tracked_save_kwargs(track))
            if created_log:
                self.logger.info(created_log, name)
            return obj
        except Exception as exc:
            self.logger.error("Failed to create %s: %s", name, exc)
            return None

    async def _find_rule_by_name(self, kind: Any, policy_id: str, rule_name: str) -> Any | None:
        """The ``kind`` rule named ``rule_name`` in policy ``policy_id``, or None (one read per rule)."""
        rules = await self.client.filters(kind=kind, policy__ids=[policy_id], name__value=rule_name)
        return rules[0] if rules else None
