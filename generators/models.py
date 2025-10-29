from __future__ import annotations

from typing import Any, List, Optional

from pydantic import BaseModel, Field, field_validator, model_validator


class BaseInfrahubModel(BaseModel):
    """A custom Pydantic base model that automatically cleans raw GraphQL data before validation."""

    @model_validator(mode="before")
    @classmethod
    def _pre_root_clean_data(cls, data: Any) -> Any:
        """The entry point for the pre-validation data cleaning."""
        return cls._recursive_clean(data)

    @classmethod
    def _recursive_clean(cls, data: Any) -> Any:
        """
        Recursively cleans raw GraphQL dictionary data.
        This mimics the logic of the `clean_data` function, unwrapping
        common GraphQL patterns like {'value': X}, {'edges': [...]}, and {'node': ...}.
        """
        if isinstance(data, dict):
            if "value" in data and len(data) == 1:
                return cls._recursive_clean(data["value"])
            if "node" in data and len(data) == 1:
                return cls._recursive_clean(data["node"])
            if "edges" in data and len(data) == 1:
                return cls._recursive_clean(data["edges"])
            return {key: cls._recursive_clean(value) for key, value in data.items()}
        if isinstance(data, list):
            return [cls._recursive_clean(item) for item in data]
        return data

    class Config:
        extra = "ignore"
        from_attributes = True


# =============================================================================
# Models for Generators
# =============================================================================


class Interface(BaseModel):
    name: str
    role: Optional[str] = None


class Template(BaseModel):
    id: str
    interfaces: List[Interface] = Field(default_factory=list)


class Parent(BaseModel):
    id: str
    name: str
    amount_of_super_spines: int
    spine_interface_sorting_method: str
    super_spine_template: Template


class DeploymentDesign(BaseModel):
    """Represents a single deployment within a design."""

    id: str
    name: str
    amount_of_spines: Optional[int] = None
    spine_switch_template: Optional[Template] = None
    parent: Optional[Parent] = None
    amount_of_super_spines: Optional[int] = None
    super_spine_template: Optional[Template] = None
    index: Optional[int] = None

    @field_validator("id", "name", check_fields=False)
    def check_string_fields(cls, v: str) -> str:
        """Ensure id and name are non-empty strings."""
        if not isinstance(v, str) or not v.strip():
            raise ValueError("Field must be a non-empty string.")
        return v

    @field_validator(
        "amount_of_spines",
        "amount_of_super_spines",
        "index",
        check_fields=False,
    )
    def check_integer_fields(cls, v: Optional[int]) -> Optional[int]:
        """Ensure integer fields are non-negative if they are set."""
        if v is not None and v < 0:
            raise ValueError("Field must be a non-negative integer.")
        return v
