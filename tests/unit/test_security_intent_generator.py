"""Unit tests for SecurityIntentRule generator materialization."""

from __future__ import annotations

import asyncio
from types import SimpleNamespace
from typing import Any
from unittest.mock import AsyncMock, MagicMock

from generators.topology.security_intent import SecurityIntentRuleGenerator


def _make_gen() -> Any:
    gen = SecurityIntentRuleGenerator.__new__(SecurityIntentRuleGenerator)
    gen.client = AsyncMock()
    gen.logger = MagicMock()
    return gen


def _rule_obj(rule_id: str = "r1", index: int = 100) -> Any:
    obj = MagicMock()
    obj.id = rule_id
    obj.index = SimpleNamespace(value=index)
    obj.save = AsyncMock()
    return obj


class TestSecurityIntentGenerator:
    def test_renders_policy_rule_and_derives_prefixes_from_ips(self) -> None:
        gen = _make_gen()

        gen.client.filters = AsyncMock(side_effect=[[], []])
        policy_rule = _rule_obj(rule_id="spr-1", index=110)
        gen.client.create = AsyncMock(return_value=policy_rule)

        intent = {
            "id": "intent-1",
            "name": "intent-web-to-app",
            "match_mode": "ip_exact",
            "priority": 110,
            "action": "permit",
            "protocol": "tcp",
            "port_start": 443,
            "port_end": None,
            "log": True,
            "enabled": True,
            "description": "test",
            "policy": {"id": "pol-1", "name": "policy-1"},
            "source_zone": {"id": "z1", "name": "WEB"},
            "destination_zone": {"id": "z2", "name": "APP"},
            "source_segments": [],
            "destination_segments": [],
            "source_ip_addresses": [
                {"id": "ip-src-1", "ip_prefix": {"id": "pfx-src-1"}},
            ],
            "destination_ip_addresses": [
                {"id": "ip-dst-1", "ip_prefix": {"id": "pfx-dst-1"}},
            ],
            "source_prefixes": [],
            "destination_prefixes": [],
            "source_tag": {},
            "destination_tag": {},
            "security_profile": {},
        }

        asyncio.run(gen._render_intent_rule(intent))

        gen.client.create.assert_called_once()
        kwargs = gen.client.create.call_args.kwargs
        assert kwargs["kind"] == "SecurityPolicyRule"
        data = kwargs["data"]
        assert data["source_ip_addresses"] == [{"id": "ip-src-1"}]
        assert data["destination_ip_addresses"] == [{"id": "ip-dst-1"}]
        assert data["source_prefixes"] == [{"id": "pfx-src-1"}]
        assert data["destination_prefixes"] == [{"id": "pfx-dst-1"}]

    def test_renders_tag_rule_when_tags_present(self) -> None:
        gen = _make_gen()

        gen.client.filters = AsyncMock(side_effect=[[], [], []])
        policy_rule = _rule_obj(rule_id="spr-2", index=120)
        tag_rule = _rule_obj(rule_id="str-1", index=0)
        gen.client.create = AsyncMock(side_effect=[policy_rule, tag_rule])

        intent = {
            "id": "intent-2",
            "name": "intent-dmz-to-web",
            "match_mode": "label",
            "priority": 120,
            "action": "permit",
            "protocol": "tcp",
            "port_start": 443,
            "port_end": None,
            "log": False,
            "enabled": True,
            "description": "test tag",
            "policy": {"id": "pol-2", "name": "policy-2"},
            "source_zone": {},
            "destination_zone": {},
            "source_segments": [],
            "destination_segments": [],
            "source_ip_addresses": [],
            "destination_ip_addresses": [],
            "source_prefixes": [],
            "destination_prefixes": [],
            "source_tag": {"id": "tag-dmz", "name": "dmz"},
            "destination_tag": {"id": "tag-web", "name": "web-tier"},
            "security_profile": {},
        }

        asyncio.run(gen._render_intent_rule(intent))

        assert gen.client.create.call_count == 2
        first = gen.client.create.call_args_list[0].kwargs
        second = gen.client.create.call_args_list[1].kwargs
        assert first["kind"] == "SecurityPolicyRule"
        assert second["kind"] == "SecurityTagRule"
        assert second["data"]["source_tag"] == {"id": "tag-dmz"}
        assert second["data"]["destination_tag"] == {"id": "tag-web"}

    def test_updates_existing_tag_rule_for_same_tag_pair(self) -> None:
        gen = _make_gen()

        gen.client.filters = AsyncMock(side_effect=[[], [], [self._existing_tag_rule()]])
        policy_rule = _rule_obj(rule_id="spr-3", index=130)
        gen.client.create = AsyncMock(return_value=policy_rule)

        intent = {
            "id": "intent-3",
            "name": "intent-existing-tag-rule",
            "match_mode": "label",
            "priority": 130,
            "action": "deny",
            "protocol": "tcp",
            "port_start": 443,
            "port_end": None,
            "log": True,
            "enabled": True,
            "description": "existing tag rule",
            "policy": {"id": "pol-3", "name": "policy-3"},
            "source_zone": {},
            "destination_zone": {},
            "source_segments": [],
            "destination_segments": [],
            "source_ip_addresses": [],
            "destination_ip_addresses": [],
            "source_prefixes": [],
            "destination_prefixes": [],
            "source_tag": {"id": "tag-a", "name": "a"},
            "destination_tag": {"id": "tag-b", "name": "b"},
            "security_profile": {},
        }

        asyncio.run(gen._render_intent_rule(intent))

        # Only policy rule is created; tag rule is updated in place.
        assert gen.client.create.call_count == 1

    @staticmethod
    def _existing_tag_rule() -> Any:
        tag_rule = MagicMock()
        tag_rule.action = SimpleNamespace(value="permit")
        tag_rule.log = SimpleNamespace(value=False)
        tag_rule.description = SimpleNamespace(value="old")
        tag_rule.save = AsyncMock()
        return tag_rule
