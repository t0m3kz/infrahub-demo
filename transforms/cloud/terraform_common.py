"""Shared helpers for the per-provider Terraform HCL renderers."""


def tf_id(name: str) -> str:
    return name.lower().replace("-", "_").replace(" ", "_").replace(".", "_")
