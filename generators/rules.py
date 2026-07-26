"""Generic mixin for indexed rule lifecycle operations."""

from __future__ import annotations

from datetime import datetime, timedelta, timezone
from typing import Any, Protocol


class RuleLifecycleMixin:
    """Reusable helpers for create/update of indexed policy-style rules."""

    class _Runtime(Protocol):
        client: Any
        logger: Any

    @staticmethod
    def _default_expiry_iso(validity_days: int) -> str:
        """Return UTC ISO timestamp validity_days in the future."""
        return (datetime.now(timezone.utc) + timedelta(days=validity_days)).replace(microsecond=0).isoformat()

    @staticmethod
    def _normalize_datetime_value(value: Any) -> str | None:
        """Normalize datetime-like value to ISO string consumable by fromisoformat."""
        if value is None:
            return None
        if isinstance(value, datetime):
            dt_value = value
            if dt_value.tzinfo is None:
                dt_value = dt_value.replace(tzinfo=timezone.utc)
            return dt_value.replace(microsecond=0).isoformat()

        raw = str(value).strip()
        if not raw:
            return None
        if raw.endswith("Z"):
            return raw[:-1] + "+00:00"
        return raw

    @classmethod
    def _is_expired_datetime(cls, value: Any) -> bool:
        """Check whether datetime-like value is <= current UTC time."""
        normalized = cls._normalize_datetime_value(value)
        if not normalized:
            return False
        try:
            expires_at = datetime.fromisoformat(normalized)
        except ValueError:
            return False

        if expires_at.tzinfo is None:
            expires_at = expires_at.replace(tzinfo=timezone.utc)
        return expires_at <= datetime.now(timezone.utc)

    async def _create_or_update_indexed_rule(
        self: _Runtime,
        *,
        rule_kind: Any,
        parent_id: str,
        rule_name: str,
        rule_data: dict[str, Any],
        find_existing: Any,
        allocate_index: Any,
        index_attr: str = "index",
        expires_attr: str = "expires_at",
        disabled_attr: str = "disabled",
        collision_hint: str = "policy-index",
        max_attempts: int = 5,
        default_validity_days: int = 180,
    ) -> tuple[Any, int]:
        """Create/update an indexed rule with retry on uniqueness collision."""
        existing_rule = await find_existing(parent_id, rule_name)
        if existing_rule is not None:
            existing_index_attr = getattr(existing_rule, index_attr, None)
            existing_index = (
                int(existing_index_attr.value)
                if existing_index_attr is not None and getattr(existing_index_attr, "value", None) is not None
                else await allocate_index(parent_id)
            )

            payload = dict(rule_data)
            payload["id"] = existing_rule.id
            payload[index_attr] = existing_index

            existing_expires_attr = getattr(existing_rule, expires_attr, None)
            existing_expires_at = getattr(existing_expires_attr, "value", None) if existing_expires_attr else None
            if expires_attr not in payload:
                payload[expires_attr] = existing_expires_at or RuleLifecycleMixin._default_expiry_iso(
                    default_validity_days
                )

            existing_disabled_attr = getattr(existing_rule, disabled_attr, None)
            existing_disabled = (
                bool(getattr(existing_disabled_attr, "value", False)) if existing_disabled_attr else False
            )
            payload[disabled_attr] = bool(
                existing_disabled
                or payload.get(disabled_attr, False)
                or RuleLifecycleMixin._is_expired_datetime(payload.get(expires_attr))
            )

            rule = await self.client.create(kind=rule_kind, data=payload)
            await rule.save(allow_upsert=True)
            return rule, existing_index

        last_exc: Exception | None = None
        for attempt in range(1, max_attempts + 1):
            payload = dict(rule_data)
            payload[index_attr] = await allocate_index(parent_id)
            if expires_attr not in payload:
                payload[expires_attr] = RuleLifecycleMixin._default_expiry_iso(default_validity_days)
            payload[disabled_attr] = bool(
                payload.get(disabled_attr, False) or RuleLifecycleMixin._is_expired_datetime(payload.get(expires_attr))
            )

            try:
                rule = await self.client.create(kind=rule_kind, data=payload)
                await rule.save(allow_upsert=True)
                return rule, int(payload[index_attr])
            except Exception as exc:
                if collision_hint in str(exc) and attempt < max_attempts:
                    self.logger.warning(
                        "Index collision for '%s', retrying with refreshed index (attempt %d/%d)",
                        rule_name,
                        attempt + 1,
                        max_attempts,
                    )
                    last_exc = exc
                    continue
                raise

        assert last_exc is not None
        raise last_exc
