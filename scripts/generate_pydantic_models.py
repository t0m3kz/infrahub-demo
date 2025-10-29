#!/usr/bin/env python
"""
A tool to automatically generate Pydantic models from GraphQL queries.

This script parses a GraphQL query file, simulates the data cleaning logic
from `generators/common.py`, and generates corresponding Pydantic models.

Usage:
    uv run python scripts/generate_pydantic_models.py <path_to_query.gql> <RootModelName> <QueryKey>

Example:
    uv run python scripts/generate_pydantic_models.py queries/topology/dc.gql DcDesign TopologyDataCenter
"""

import sys
from collections import deque
from typing import Deque, Dict, List, Set, Union

from graphql import (
    DocumentNode,
    FieldNode,
    OperationDefinitionNode,
    SelectionNode,
    parse,
)


def graphql_to_pydantic_type(graphql_type: str) -> str:
    """Maps GraphQL scalar types to Python types."""
    type_map = {
        "String": "str",
        "Int": "int",
        "Float": "float",
        "Boolean": "bool",
        "ID": "str",
    }
    # Default to str for unknown types, as they are often custom scalars.
    return type_map.get(graphql_type, "str")


def to_pascal_case(snake_str: str) -> str:
    """Converts a snake_case string to PascalCase for class names."""
    return "".join(word.capitalize() for word in snake_str.split("_"))


class ModelGenerator:
    """
    Parses a GraphQL AST and generates Pydantic model definitions.
    """

    def __init__(self, root_model_name: str, query_key: str):
        self.root_model_name = root_model_name
        self.query_key = query_key
        self.models: Dict[str, str] = {}
        self.generated_classes: Set[str] = set()
        self.processing_queue: Deque[tuple[str, List[SelectionNode]]] = deque()

    def generate_models_from_query(self, query_string: str) -> str:
        """
        Main entry point to generate Pydantic models from a GraphQL query string.
        """
        ast = parse(query_string)

        # Find the main operation definition
        operation_def = self._find_operation_definition(ast)
        if not operation_def:
            return "# No operation definition found in the query."

        # Find the root field based on the query_key
        root_field = self._find_root_field(operation_def, self.query_key)
        if not root_field or not root_field.selection_set:
            return f"# No field matching the query key '{self.query_key}' found."

        # The actual data is inside edges -> node
        try:
            node_selections = (
                root_field.selection_set.selections[0]
                .selection_set.selections[0]
                .selection_set.selections
            )
        except (AttributeError, IndexError):
            return "# Could not find the 'edges -> node' structure in the query."

        # Start processing from the root model
        self.processing_queue.append((self.root_model_name, node_selections))
        self.generated_classes.add(self.root_model_name)

        while self.processing_queue:
            model_name, selections = self.processing_queue.popleft()
            model_definition = self._create_model_definition(model_name, selections)
            self.models[model_name] = model_definition

        # Assemble the final output string
        header = [
            "from __future__ import annotations",
            "from typing import List, Optional",
            "from pydantic import BaseModel, Field",
            "\n\n# Pydantic models generated from GraphQL query\n",
        ]

        # Output models in the order they were defined
        ordered_models = list(self.models.keys())

        return "\n".join(header + [self.models[name] for name in ordered_models])

    def _create_model_definition(
        self, model_name: str, selections: List[SelectionNode]
    ) -> str:
        """
        Creates a single Pydantic model class definition string.
        """
        fields_str = []
        for selection in selections:
            if isinstance(selection, FieldNode):
                field_name = (
                    selection.alias.value if selection.alias else selection.name.value
                )

                # Simulate clean_data: if selection is just a 'value', treat as scalar
                if (
                    selection.selection_set
                    and len(selection.selection_set.selections) == 1
                    and getattr(selection.selection_set.selections[0], "name", {}).get(
                        "value"
                    )
                    == "value"
                ):
                    field_type = "str  # Assuming scalar from '{ value }' pattern"
                    fields_str.append(f"    {field_name}: {field_type}")
                    continue

                # Simulate clean_data: if selection is 'edges { node { ... } }', treat as List[SubModel]
                if (
                    selection.selection_set
                    and len(selection.selection_set.selections) == 1
                    and getattr(selection.selection_set.selections[0], "name", {}).get(
                        "value"
                    )
                    == "edges"
                ):
                    try:
                        sub_selections = (
                            selection.selection_set.selections[0]
                            .selection_set.selections[0]
                            .selection_set.selections
                        )
                        sub_model_name = to_pascal_case(field_name) + "Item"
                        field_type = f"List[{sub_model_name}]"

                        if sub_model_name not in self.generated_classes:
                            self.processing_queue.append(
                                (sub_model_name, sub_selections)
                            )
                            self.generated_classes.add(sub_model_name)
                    except (AttributeError, IndexError):
                        # Not the expected structure, treat as generic
                        field_type = "Any"

                # Nested object, create a sub-model
                elif selection.selection_set:
                    sub_model_name = to_pascal_case(field_name)
                    field_type = sub_model_name
                    if sub_model_name not in self.generated_classes:
                        self.processing_queue.append(
                            (sub_model_name, selection.selection_set.selections)
                        )
                        self.generated_classes.add(sub_model_name)

                # Scalar field
                else:
                    field_type = "str  # Assuming scalar"

                fields_str.append(f"    {field_name}: {field_type}")

        model_body = "\n".join(fields_str) if fields_str else "    pass"
        return f"class {model_name}(BaseModel):\n{model_body}\n"

    def _find_operation_definition(
        self, ast: DocumentNode
    ) -> OperationDefinitionNode | None:
        for definition in ast.definitions:
            if isinstance(definition, OperationDefinitionNode):
                return definition
        return None

    def _find_root_field(
        self, op_def: OperationDefinitionNode, key: str
    ) -> FieldNode | None:
        for selection in op_def.selection_set.selections:
            if isinstance(selection, FieldNode) and selection.name.value == key:
                return selection
        return None


def main():
    """Main function to run the script."""
    if len(sys.argv) != 4:
        print(__doc__)
        sys.exit(1)

    query_file_path = sys.argv[1]
    root_model_name = sys.argv[2]
    query_key = sys.argv[3]

    try:
        with open(query_file_path, "r") as f:
            query_string = f.read()
    except FileNotFoundError:
        print(f"Error: Query file not found at '{query_file_path}'")
        sys.exit(1)

    generator = ModelGenerator(root_model_name=root_model_name, query_key=query_key)
    pydantic_code = generator.generate_models_from_query(query_string)

    print(
        "# NOTE: This is a generated file. You may need to manually add a `BaseModel` import,"
    )
    print("# adjust types (e.g., Optional, specific scalars), and add field aliases.\n")
    print(pydantic_code)


if __name__ == "__main__":
    main()
