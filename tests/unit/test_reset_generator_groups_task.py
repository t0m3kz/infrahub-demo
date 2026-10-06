"""Unit tests for `invoke reset-generator-groups` in tasks.py."""

from __future__ import annotations

from types import SimpleNamespace
from typing import Any
from unittest.mock import MagicMock, patch

import pytest
from invoke import Context, Exit

import tasks

HASH = "0123456789abcdef0123456789abcdef"
DEFINITIONS = ("add_dc", "add_app_component", "add_app_component_segment")


def _group(name: str, kind: str, member_kinds: list[str]) -> MagicMock:
    """Return a group node stand-in with one peer per entry in `member_kinds`."""
    group = MagicMock()
    group.name.value = name
    group.get_kind.return_value = kind
    group.members.peers = [SimpleNamespace(id=f"{name}-m{i}", typename=k) for i, k in enumerate(member_kinds)]

    def _remove(relation_to_update: str, related_nodes: list[str]) -> None:
        group.members.peers = [peer for peer in group.members.peers if peer.id not in related_nodes]

    group.remove_relationships.side_effect = _remove
    return group


def _client(groups: list[MagicMock], branches: tuple[str, ...] = ("main",)) -> MagicMock:
    """Return a sync client whose `all()` answers per kind, as the SDK would."""
    client = MagicMock()
    client.branch.all.return_value = {name: object() for name in branches}

    def _all(kind: str, **_: Any) -> list[Any]:
        if kind == "CoreGeneratorDefinition":
            return [SimpleNamespace(name=SimpleNamespace(value=name)) for name in DEFINITIONS]
        return [group for group in groups if group.get_kind() == kind]

    client.all.side_effect = _all
    return client


@pytest.fixture
def groups() -> dict[str, MagicMock]:
    """A tracking group of each kind, plus target and lock groups that must survive."""
    return {
        "aware": _group(f"add_dc-{HASH}", "CoreGeneratorAwareGroup", ["DcimPhysicalDevice", "DcimCable", "DcimCable"]),
        "generator": _group(f"add_app_component_segment-{HASH}", "CoreGeneratorGroup", ["ManagedVlanDomainSegment"]),
        # Bootstrap target group of a generator kind: no params-hash suffix.
        "target": _group("customer_deployments_dc", "CoreGeneratorGroup", ["TopologyCustomerDC"]),
        # Hash-suffixed, but no generator definition by that name.
        "unknown": _group(f"retired_generator-{HASH}", "CoreGeneratorAwareGroup", ["DcimCable"]),
        "standard": _group(f"add_dc-{HASH}", "CoreStandardGroup", ["TopologyDataCenter"]),
        "lock": _group("lock-pool-alloc-fabric-dc-1", "CoreStandardGroup", []),
    }


def _run(client: MagicMock, **kwargs: Any) -> None:
    """Run the task with the client patched in."""
    with patch.object(tasks, "_infrahub_client", return_value=client):
        tasks.reset_generator_groups(MagicMock(spec=Context), **kwargs)


class TestIsTrackingGroup:
    """Group-name classification."""

    @pytest.mark.parametrize(
        ("name", "expected"),
        [
            (f"add_dc-{HASH}", True),
            (f"add_app_component-{HASH}", True),
            (f"add_app_component_segment-{HASH}", True),
            ("add_dc", False),
            ("topologies_dc", False),
            (f"lock-add_dc-{HASH}", False),
            (f"add_dc-{HASH[:-1]}", False),
            (f"add_dc-{HASH.upper()}", False),
        ],
    )
    def test_classifies_by_definition_and_hash(self, name: str, expected: bool) -> None:
        """Only "<known definition>-<md5 hex>" is a tracking group."""
        assert tasks._is_tracking_group(name, set(DEFINITIONS)) is expected


class TestResetGeneratorGroups:
    """The task empties generator tracking groups and nothing else."""

    def test_clears_members_of_tracking_groups_only(self, groups: dict[str, MagicMock]) -> None:
        """Both generator group kinds are emptied; target, unknown, standard and lock groups are untouched."""
        _run(_client(list(groups.values())), branch="b1")

        groups["aware"].remove_relationships.assert_called_once_with(
            relation_to_update="members", related_nodes=[f"add_dc-{HASH}-m{i}" for i in range(3)]
        )
        groups["generator"].remove_relationships.assert_called_once_with(
            relation_to_update="members", related_nodes=[f"add_app_component_segment-{HASH}-m0"]
        )
        for key in ("target", "unknown", "standard", "lock"):
            groups[key].remove_relationships.assert_not_called()

    def test_queries_only_generator_group_kinds_on_branch(self, groups: dict[str, MagicMock]) -> None:
        """Groups are listed per generator kind on the requested branch, members included."""
        client = _client(list(groups.values()))
        _run(client, branch="b1")

        group_calls = [c for c in client.all.call_args_list if c.kwargs["kind"] != "CoreGeneratorDefinition"]
        # Each pass lists every generator kind once; a confirming pass repeats the listing.
        assert {c.kwargs["kind"] for c in group_calls} == set(tasks.GENERATOR_GROUP_KINDS)
        assert all(c.kwargs["branch"] == "b1" and c.kwargs["include"] == ["members"] for c in group_calls)

    def test_dry_run_writes_nothing(self, groups: dict[str, MagicMock], caplog: pytest.LogCaptureFixture) -> None:
        """Dry run reports name, member count and kind histogram, and removes nothing."""
        caplog.set_level("INFO", logger="tasks")
        _run(_client(list(groups.values())), branch="b1", dry_run=True)

        for group in groups.values():
            group.remove_relationships.assert_not_called()
            group.save.assert_not_called()
            group.delete.assert_not_called()
        assert f"add_dc-{HASH} (CoreGeneratorAwareGroup): 3 member(s) DcimCable=2, DcimPhysicalDevice=1" in caplog.text
        assert "would remove 4 tracking-group member(s)" in caplog.text
        assert "customer_deployments_dc" not in caplog.text

    def test_large_group_is_removed_in_chunks(self) -> None:
        """Members go out in RelationshipRemove batches of _MEMBER_REMOVE_CHUNK."""
        group = _group(f"add_dc-{HASH}", "CoreGeneratorAwareGroup", ["DcimCable"] * (tasks._MEMBER_REMOVE_CHUNK + 1))
        _run(_client([group]), branch="b1")

        batches = [c.kwargs["related_nodes"] for c in group.remove_relationships.call_args_list]
        assert [len(batch) for batch in batches] == [tasks._MEMBER_REMOVE_CHUNK, 1]

    def test_empty_group_is_skipped(self) -> None:
        """A group that is already empty issues no mutation."""
        group = _group(f"add_dc-{HASH}", "CoreGeneratorAwareGroup", [])
        _run(_client([group]), branch="b1")
        group.remove_relationships.assert_not_called()

    def test_all_branches_visits_every_branch(self, groups: dict[str, MagicMock]) -> None:
        """--all-branches resets each branch the server lists."""
        client = _client([groups["aware"]], branches=("main", "b1"))
        _run(client, all_branches=True)

        branches = {c.kwargs["branch"] for c in client.all.call_args_list}
        assert branches == {"main", "b1"}

    def test_repeats_until_members_left_behind_are_gone(self) -> None:
        """A member the server didn't release on the first pass is removed by a later one."""
        group = _group(f"add_dc-{HASH}", "CoreGeneratorAwareGroup", ["ManagedMLAG", "RoutingPassword"])
        sticky = list(group.members.peers)
        calls: list[list[str]] = []

        def _remove_one_per_call(relation_to_update: str, related_nodes: list[str]) -> None:
            calls.append(list(related_nodes))
            group.members.peers = [peer for peer in group.members.peers if peer.id != related_nodes[0]]

        group.remove_relationships.side_effect = _remove_one_per_call
        _run(_client([group]), branch="b1")

        assert group.members.peers == []
        assert calls == [[peer.id for peer in sticky], [sticky[1].id]]

    def test_gives_up_when_members_never_go(self) -> None:
        """Members that survive every pass fail the task loudly instead of looping forever."""
        group = _group(f"add_dc-{HASH}", "CoreGeneratorAwareGroup", ["ManagedMLAG"])
        group.remove_relationships.side_effect = None
        with pytest.raises(Exit):
            _run(_client([group]), branch="b1")
        assert group.remove_relationships.call_count == tasks._RESET_MAX_PASSES + 1

    def test_requires_branch_or_all_branches(self) -> None:
        """Without a branch selector the task refuses to run."""
        client = _client([])
        with pytest.raises(Exit):
            _run(client)
        client.all.assert_not_called()
