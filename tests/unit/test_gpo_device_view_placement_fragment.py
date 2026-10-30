"""The query fragment queries/fragments/group_policy.gql SecurityTagPlacementFields is what component_coverage reads."""

from __future__ import annotations

from pathlib import Path
from typing import Any

import pytest
from graphql import parse
from graphql.language import FieldNode, FragmentDefinitionNode, FragmentSpreadNode, InlineFragmentNode, SelectionNode

from generators.topology.endpoint import ENDPOINT_NIC_ROLES
from tests.smoke.fixture_builders.group_policy import _gp_instance
from tests.unit.gpo_device_view_helpers import _coverage, _nic, _placed_roots


class _Spy(dict):
    """Cleaned query data that records the path of every key read from it, and from the dicts below it."""

    def __init__(self, data: dict, path: tuple[str, ...], log: set[tuple[str, ...]]) -> None:
        super().__init__(data)
        self._path, self._log = path, log

    def get(self, key: Any, default: Any = None, /) -> Any:
        self._log.add((*self._path, key))
        return self._wrap(key, super().get(key, default))

    def __getitem__(self, key: Any) -> Any:
        self._log.add((*self._path, key))
        return self._wrap(key, super().__getitem__(key))

    def _wrap(self, key: str, value: Any) -> Any:
        return _spied(value, (*self._path, key), self._log)


def _spied(value: Any, path: tuple[str, ...], log: set[tuple[str, ...]]) -> Any:
    if isinstance(value, dict):
        return _Spy(value, path, log)
    if isinstance(value, list):
        return [_spied(item, path, log) for item in value]
    return value


class TestPlacementFragment:
    """queries/fragments/group_policy.gql SecurityTagPlacementFields is tied to the code that reads its data."""

    # clean_data collapses ``edges { node }``, ``node`` and ``{ value }``, and renames ``__typename`` to ``typename``.
    COLLAPSED = frozenset({"edges", "node", "value"})
    # AppInstance kind -> the inline fragment of the placement fragment that must select its host's NICs.
    HOST_FRAGMENT = {
        "DcimPhysicalDevice": "DcimDevice",
        "ManagedControllerPhysical": "DcimDevice",
        "DcimVirtualDevice": "DcimVirtualDevice",
        "ManagedControllerVirtual": "ManagedControllerVirtual",
    }

    @classmethod
    def _paths(
        cls,
        selections: Any,
        fragments: dict[str, FragmentDefinitionNode],
        prefix: tuple[str, ...] = (),
    ) -> set[tuple[str, ...]]:
        """Every field path below ``selections`` as the cleaned data names it (inline fragments merged, named
        spreads resolved from ``fragments``)."""
        found: set[tuple[str, ...]] = set()
        for selection in selections:
            if isinstance(selection, FragmentSpreadNode):
                found |= cls._paths(fragments[selection.name.value].selection_set.selections, fragments, prefix)
            elif isinstance(selection, InlineFragmentNode):
                found |= cls._paths(selection.selection_set.selections, fragments, prefix)
            elif isinstance(selection, FieldNode):
                name = (selection.alias or selection.name).value
                path = prefix if name in cls.COLLAPSED else (*prefix, "typename" if name == "__typename" else name)
                found |= ({path} if path else set()) | cls._paths(
                    selection.selection_set.selections if selection.selection_set else (), fragments, path
                )
        return found

    @staticmethod
    def _fragments(root_dir: Path) -> dict[str, FragmentDefinitionNode]:
        """Every fragment of queries/fragments/group_policy.gql by name."""
        document = parse((root_dir / "queries/fragments/group_policy.gql").read_text())
        return {item.name.value: item for item in document.definitions if isinstance(item, FragmentDefinitionNode)}

    @classmethod
    def _fragment(cls, root_dir: Path, name: str) -> FragmentDefinitionNode:
        return cls._fragments(root_dir)[name]

    @staticmethod
    def _sub(selections: Any, name: str) -> Any:
        """The selections of the field ``name`` among ``selections``."""
        field = next(item for item in selections if isinstance(item, FieldNode) and item.name.value == name)
        assert field.selection_set is not None
        return field.selection_set.selections

    @classmethod
    def _below(cls, selections: Any, *names: str) -> list[SelectionNode]:
        """The selections of the node reached through the connections ``names`` (``edges { node }`` each)."""
        for name in names:
            selections = cls._sub(cls._sub(cls._sub(selections, name), "edges"), "node")
        return list(selections)

    @staticmethod
    def _instance(kind: str, name: str, nics: list[dict]) -> dict:
        found = _gp_instance(name, nics, host=name if "Virtual" in kind else None)
        found["__typename"] = kind
        return found

    def _recorded_reads(self, kind: str) -> set[tuple[str, ...]]:
        """The key paths below a SecurityTag that component_coverage reads for one instance of ``kind``, host
        cabled through a plain port and a LAG member (port-channel)."""
        nics = [
            _nic("srv-web", "eno1", "dc1-leaf-01", "Ethernet10"),
            _nic("srv-web", "eno2", "dc1-leaf-01", "Ethernet20", lag="port-channel20"),
            _nic("srv-web", "eno3", "bl-dc12-01", "Ethernet5"),
        ]
        roots = _placed_roots(web=[self._instance(kind, "srv-web", nics)], db=[])
        log: set[tuple[str, ...]] = set()
        roots["SecurityTag"] = _spied(roots["SecurityTag"], (), log)
        coverage = _coverage(roots)
        assert coverage.claims, "the spied data must be placed, or the read paths are a prefix of the real ones"
        return log

    @pytest.mark.parametrize("kind", sorted(HOST_FRAGMENT))
    def test_the_fragment_selects_every_key_component_coverage_reads(self, root_dir: Path, kind: str) -> None:
        """Every key the coverage reads below a tag must be selected: the instance's own fields and the inline
        fragment of ITS kind (a path selected only for another kind does not count), the rest by the two fragments."""
        placement = self._fragment(root_dir, "SecurityTagPlacementFields").selection_set.selections
        shared = self._fragment(root_dir, "SecurityTagGroupPolicyFields").selection_set.selections
        instance = self._below(placement, "components", "instances")
        inline = [item for item in instance if isinstance(item, InlineFragmentNode)]
        assert {item.type_condition.name.value for item in inline} == set(self.HOST_FRAGMENT.values())
        own_kind = [item for item in inline if item.type_condition.name.value == self.HOST_FRAGMENT[kind]]
        fragments = self._fragments(root_dir)
        instance_paths = self._paths(
            [item for item in instance if item not in inline] + own_kind, fragments, ("components", "instances")
        )
        tag_paths = self._paths(placement, fragments) | self._paths(shared, fragments)

        reads = self._recorded_reads(kind)
        host = ("components", "instances") + (("hosting_device",) if "Virtual" in kind else ())
        assert (*host, "interfaces", "cable", "endpoints", "lag", "segments", "id") in reads, (
            "the data must exercise the whole walk down to the port-channel's tags"
        )
        missing = {
            path
            for path in reads
            if path not in (instance_paths if path[:2] == ("components", "instances") else tag_paths)
            and path not in {("components",), ("components", "instances")}
        }
        assert not missing, f"{kind}: read by component_coverage but not selected for it: {sorted(missing)}"

    def test_the_nic_roles_the_fragment_filters_on_are_the_ones_the_generator_cables(self, root_dir: Path) -> None:
        fragment = self._fragment(root_dir, "SecurityTagPlacementFields")
        found: list[list[str]] = []

        def walk(selections: Any) -> None:
            for selection in selections:
                if isinstance(selection, FieldNode) and selection.name.value == "interfaces":
                    for argument in selection.arguments:
                        assert argument.name.value == "role__values"
                        found.append([value.value for value in argument.value.values])
                if getattr(selection, "selection_set", None):
                    walk(selection.selection_set.selections)

        walk(fragment.selection_set.selections)
        assert found == [list(ENDPOINT_NIC_ROLES)] * 3, "one filter per host kind"

    def test_the_nic_block_is_one_fragment_spread_under_a_field_in_each_host_branch(self, root_dir: Path) -> None:
        """HostNicFields is the single NIC selection: spread as ``interfaces(...) { edges { node { ...F } } }`` of
        each host kind, so the three branches cannot drift."""
        placement = self._fragment(root_dir, "SecurityTagPlacementFields").selection_set.selections
        instance = self._below(placement, "components", "instances")
        spread_in: list[str] = []
        for branch in (item for item in instance if isinstance(item, InlineFragmentNode)):
            selections = branch.selection_set.selections
            if branch.type_condition.name.value != "DcimDevice":
                selections = self._sub(self._sub(selections, "hosting_device"), "node")
            node = self._below(selections, "interfaces")
            assert len(node) == 1
            spread = node[0]
            assert isinstance(spread, FragmentSpreadNode)
            spread_in.append(f"{branch.type_condition.name.value}:{spread.name.value}")
        assert spread_in == [
            f"{kind}:HostNicFields" for kind in ("DcimDevice", "DcimVirtualDevice", "ManagedControllerVirtual")
        ]

    def test_no_named_spread_sits_directly_under_an_inline_fragment(self, root_dir: Path) -> None:
        """``... on X { ...F }`` resolves to nulls on a live Infrahub (commit 7a38de0e); a spread under a field
        inside the inline fragment (``node { ...F }``) works. Holds for every fragment of the file."""
        offenders = [
            f"{name}: ... on {inline.type_condition.name.value} {{ ...{spread.name.value} }}"
            for name, definition in self._fragments(root_dir).items()
            for inline in self._inline_fragments(definition)
            for spread in inline.selection_set.selections
            if isinstance(spread, FragmentSpreadNode)
        ]
        assert not offenders, offenders

    @classmethod
    def _inline_fragments(cls, node: Any) -> list[InlineFragmentNode]:
        """Every inline fragment at any depth below ``node``."""
        selection_set = getattr(node, "selection_set", None)
        found: list[InlineFragmentNode] = []
        for selection in selection_set.selections if selection_set else ():
            if isinstance(selection, InlineFragmentNode):
                found.append(selection)
            found += cls._inline_fragments(selection)
        return found
