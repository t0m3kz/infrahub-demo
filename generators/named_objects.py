"""Generic mixin for get-or-create-by-name and rule-by-name lookups."""

from __future__ import annotations

from typing import TYPE_CHECKING, Any

if TYPE_CHECKING:
    import logging


class GetOrCreateByNameMixin:
    """Shared get-or-create-by-name-value idiom for policy/SG/zone-style catalog objects.

    Expects the host class to provide: ``client``, ``logger`` (present on
    ``CommonGenerator``).
    """

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
        """Filter by name__value, upsert-and-return if found; else create,
        save, and return. Shared by every policy/SG/zone lookup that used to
        hand-roll this same try/filter/try/create dance independently.

        ``track`` is the ownership decision, made per call site: True when
        the object belongs to the target this run is for (the run's tracking
        group claims it and a later run that stops producing it deletes it);
        False for a catalog object several targets reach — a zone shared by
        every segment of an environment, a source segment's policy shared by
        every application with a component there — which is then written
        with update_group_context=False so no single run can claim, and later
        delete, it for everyone. Nothing cleans an untracked object up.
        An untracked object that already exists is returned without a save.
        """
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
            if track:
                await obj.save(allow_upsert=True)
            else:
                await obj.save(allow_upsert=True, update_group_context=False)
            if created_log:
                self.logger.info(created_log, name)
            return obj
        except Exception as exc:
            self.logger.error("Failed to create %s: %s", name, exc)
            return None

    async def _find_rule_by_name(self, kind: Any, policy_id: str, rule_name: str) -> Any | None:
        """The ``kind`` rule named ``rule_name`` in policy ``policy_id``, or None."""
        rules = await self.client.filters(kind=kind, policy__ids=[policy_id], name__value=rule_name)
        return rules[0] if rules else None
