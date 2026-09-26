"""Generic mixin for get-or-create-by-name lookups."""

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
    ) -> Any | None:
        """Filter by name__value, upsert-and-return if found; else create,
        save, and return. Shared by every policy/SG/zone lookup that used to
        hand-roll this same try/filter/try/create dance independently."""
        try:
            existing = await self.client.filters(kind=kind, name__value=name)
            if existing:
                if found_log:
                    self.logger.info(found_log, name)
                await existing[0].save(allow_upsert=True)
                return existing[0]
        except Exception as exc:
            self.logger.warning("Could not look up %s: %s", name, exc)

        try:
            obj = await self.client.create(kind=kind, data=create_data)
            await obj.save(allow_upsert=True)
            if created_log:
                self.logger.info(created_log, name)
            return obj
        except Exception as exc:
            self.logger.error("Failed to create %s: %s", name, exc)
            return None
