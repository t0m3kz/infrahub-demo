"""Crossplane composition function — Infrahub approval gate.

Runs as a step in a Crossplane Composition pipeline.
Blocks XR readiness until infrahub.io/approval == "approved".

If the annotation is absent or "pending", it calls the bridge /xr/sync
endpoint to create the Infrahub branch + PC (idempotent), then sets
Synced=False with a human-readable message so the XR status shows the
pending PC URL.

Once infrahub.io/approval == "approved" (patched by the bridge after PC
merge), this function does nothing and lets the next pipeline step run.

Deployment
----------
Package this file together with the crossplane Python function runner.
Add to your Composition's pipeline:

  - step: infrahub-approval-gate
    functionRef:
      name: function-infrahub-gate
    input:
      apiVersion: infrahub.io/v1alpha1
      kind: InfrahubGateConfig
      spec:
        bridgeUrl: http://crossplane-bridge.infrahub-system.svc:8090
"""

from __future__ import annotations

import logging
import os
from typing import Any

import httpx

logger = logging.getLogger("function-infrahub-gate")

BRIDGE_URL = os.getenv("INFRAHUB_BRIDGE_URL", "http://crossplane-bridge.infrahub-system.svc:8090")


def get_annotation(oxr: dict, key: str, default: str = "") -> str:
    return ((oxr.get("metadata") or {}).get("annotations") or {}).get(key, default)


def set_synced_false(rsp: dict, reason: str, message: str) -> dict:
    """Add a Synced=False condition to the XR status."""
    conditions = (
        rsp.setdefault("desired", {})
        .setdefault("composite", {})
        .setdefault("resource", {})
        .setdefault("status", {})
        .setdefault("conditions", [])
    )

    # Remove any existing Synced condition
    conditions[:] = [c for c in conditions if c.get("type") != "Synced"]
    conditions.append(
        {
            "type": "Synced",
            "status": "False",
            "reason": reason,
            "message": message,
        }
    )
    return rsp


def _build_sync_payload(oxr: dict, function_input: dict) -> dict:
    meta = oxr.get("metadata", {})
    annotations = meta.get("annotations", {})
    spec = oxr.get("spec", {})
    kind = oxr.get("kind", "")
    name = meta.get("name", "")
    namespace = meta.get("namespace", "customer")

    gvk = oxr.get("apiVersion", "/v1alpha1")
    group_version = gvk.rsplit("/", 1)
    group = group_version[0] if len(group_version) == 2 else gvk
    version = group_version[1] if len(group_version) == 2 else "v1alpha1"
    plural = kind.lower() + "s"

    xr_ref = {
        "group": group,
        "version": version,
        "plural": plural,
        "namespace": namespace,
        "name": name,
    }

    profile_spec = {
        "service_instance_id": annotations.get("platform.cloud.allianz/service-instance-id", ""),
        "oe_name": annotations.get("platform.cloud.allianz/oe-name", ""),
        "debtor": annotations.get("platform.cloud.allianz/debtor", ""),
        "cost_center": annotations.get("platform.cloud.allianz/cost-center", ""),
    }

    payload: dict[str, Any] = {
        "xr": xr_ref,
        "kind": kind,
        "profile": profile_spec,
        "service_instance_id": profile_spec["service_instance_id"],
    }

    params = spec.get("parameters", spec)

    if kind == "Proxy":
        payload["proxy"] = {
            "region": params.get("region", ""),
            "stage": params.get("stage", "dev"),
            "allowed_domains": params.get("allowedDomains", []),
            "allowed_cidrs": params.get("allowedCidrs", []),
            "network_zone": params.get("networkZone", "internet"),
            "profile": params.get("profile", ""),
        }

    elif kind == "Registration":
        payload["registration"] = {
            "repository": params.get("repository", ""),
            "network_zone": params.get("networkZone", "internet"),
            "cloud_provider": params.get("cloudProvider", "aws"),
            "region": params.get("region", ""),
            "stage": params.get("stage", "dev"),
            "ou": params.get("ou", "az-tec"),
            "profile": params.get("profile", ""),
        }

    elif kind == "Cluster":
        vcluster = spec.get("vCluster", {})
        git = spec.get("git", {})
        payload["cluster"] = {
            "name": name,
            "ingress_domain": vcluster.get("ingressDomain", ""),
            "git_url": git.get("url", ""),
            "stage": annotations.get("platform.cloud.allianz/stage", "dev"),
        }

    return payload


def compose(req: dict, rsp: dict) -> dict:
    """
    Main composition function entry point.

    req: RunFunctionRequest dict
    rsp: RunFunctionResponse dict (mutated in place)
    returns: updated rsp
    """
    oxr = req.get("observed", {}).get("composite", {}).get("resource", {})
    function_input = req.get("input", {}).get("spec", {})
    bridge_url = function_input.get("bridgeUrl", BRIDGE_URL)

    approval = get_annotation(oxr, "infrahub.io/approval")

    # Already approved — let the next pipeline step run
    if approval == "approved":
        logger.info("XR %s has infrahub approval — proceeding", oxr.get("metadata", {}).get("name"))
        return rsp

    pc_url = get_annotation(oxr, "infrahub.io/pc-url")
    pc_id = get_annotation(oxr, "infrahub.io/pc-id")

    # Not yet synced to Infrahub — call the bridge
    if not pc_id:
        try:
            payload = _build_sync_payload(oxr, function_input)
            response = httpx.post(f"{bridge_url}/xr/sync", json=payload, timeout=30)
            response.raise_for_status()
            result = response.json()
            pc_url = result.get("pc_url", "")
            logger.info("Infrahub PC created: %s", pc_url)
        except Exception as exc:
            logger.error("Bridge /xr/sync failed: %s", exc)
            return set_synced_false(
                rsp,
                reason="InfrahubBridgeError",
                message=f"Failed to create Infrahub Proposed Change: {exc}",
            )

    message = "Awaiting network team approval in Infrahub."
    if pc_url:
        message = f"Awaiting Infrahub approval. Review at: {pc_url}"

    return set_synced_false(
        rsp,
        reason="AwaitingInfrahubApproval",
        message=message,
    )
