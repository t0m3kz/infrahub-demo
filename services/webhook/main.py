"""Webhook receiver — verifies Infrahub HMAC signatures, enriches with device deploy list."""

from __future__ import annotations

import base64
import hashlib
import hmac
import logging
import os
from collections import defaultdict

import httpx
import uvicorn
from fastapi import FastAPI, HTTPException, Request

logger = logging.getLogger("webhook")
logging.basicConfig(level=logging.INFO, format="%(asctime)s %(levelname)s %(message)s")

app = FastAPI()
SHARED_KEY = os.getenv("WEBHOOK_SECRET", "change-me-in-production").encode()
INFRAHUB_URL = os.getenv("INFRAHUB_URL", "http://infrahub-server:8000")
INFRAHUB_TOKEN = os.getenv("INFRAHUB_API_TOKEN", "")

ARTIFACTS_QUERY = """
query ProposedChangeArtifacts($name: String!) {
  CoreProposedChange(name__value: $name) {
    edges {
      node {
        source_branch { value }
        validations {
          edges {
            node {
              __typename
              ... on CoreArtifactValidator {
                checks {
                  edges {
                    node {
                      ... on CoreArtifactCheck {
                        changed { value }
                        label { value }
                      }
                    }
                  }
                }
              }
            }
          }
        }
      }
    }
  }
}
"""


CREATE_THREAD_MUTATION = """
mutation CreateThread($pc_id: String!, $label: String!) {
  CoreChangeThreadCreate(data: {
    label: { value: $label }
    resolved: { value: false }
    change: { id: $pc_id }
  }) {
    object { id }
  }
}
"""

CREATE_THREAD_COMMENT_MUTATION = """
mutation CreateThreadComment($thread_id: String!, $text: String!) {
  CoreThreadCommentCreate(data: { text: { value: $text } thread: { id: $thread_id } }) {
    object { id }
  }
}
"""


async def _gql(client: httpx.AsyncClient, query: str, variables: dict) -> dict:
    resp = await client.post(
        f"{INFRAHUB_URL}/graphql",
        headers={"X-INFRAHUB-KEY": INFRAHUB_TOKEN, "Content-Type": "application/json"},
        json={"query": query, "variables": variables},
    )
    resp.raise_for_status()
    return resp.json()


async def post_thread(pc_id: str, label: str, text: str) -> None:
    if not INFRAHUB_TOKEN or not pc_id:
        return
    try:
        async with httpx.AsyncClient(timeout=15) as client:
            thread_result = await _gql(client, CREATE_THREAD_MUTATION, {"pc_id": pc_id, "label": label})
            thread_id = thread_result["data"]["CoreChangeThreadCreate"]["object"]["id"]
            await _gql(client, CREATE_THREAD_COMMENT_MUTATION, {"thread_id": thread_id, "text": text})
            logger.info("Created %s thread on pc=%s", label, pc_id)
    except Exception as exc:
        logger.warning("Failed to post thread: %s", exc)


async def fetch_changed_devices(pc_name: str) -> dict[str, list[str]]:
    if not INFRAHUB_TOKEN:
        return {}
    try:
        async with httpx.AsyncClient(timeout=15) as client:
            resp = await client.post(
                f"{INFRAHUB_URL}/graphql",
                headers={"X-INFRAHUB-KEY": INFRAHUB_TOKEN, "Content-Type": "application/json"},
                json={"query": ARTIFACTS_QUERY, "variables": {"name": pc_name}},
            )
            resp.raise_for_status()
            data = resp.json()
    except Exception as exc:
        logger.warning("Failed to fetch artifact checks: %s", exc)
        return {}

    pc_edges = data.get("data", {}).get("CoreProposedChange", {}).get("edges", [])
    if not pc_edges:
        return {}

    devices: dict[str, list[str]] = defaultdict(list)
    for val_edge in pc_edges[0]["node"].get("validations", {}).get("edges", []):
        val = val_edge["node"]
        if val.get("__typename") != "CoreArtifactValidator":
            continue
        for check_edge in val.get("checks", {}).get("edges", []):
            check = check_edge.get("node") or {}
            if not check.get("changed", {}).get("value"):
                continue
            label: str = check.get("label", {}).get("value", "")
            if ": " in label:
                device_type, device_name = label.split(": ", 1)
                devices[device_type].append(device_name)

    return dict(devices)


def verify_signature(message_id: str, timestamp: str, payload: str, received_signature: str) -> bool:
    import json as _json

    try:
        compact = _json.dumps(_json.loads(payload), separators=(",", ":"))
    except Exception:
        compact = payload
    unsigned_data = f"{message_id}.{timestamp}.{compact}".encode()
    expected = base64.b64encode(hmac.new(SHARED_KEY, unsigned_data, hashlib.sha256).digest()).decode()
    return hmac.compare_digest(f"v1,{expected}", received_signature)


@app.get("/health")
async def health_check() -> dict:
    return {"status": "ok"}


@app.post("/")
async def catch_all(request: Request) -> dict:
    headers = request.headers
    message_id = headers.get("webhook-id")
    timestamp = headers.get("webhook-timestamp")
    received_signature = headers.get("webhook-signature")
    payload_bytes = await request.body()
    payload_json = payload_bytes.decode()
    payload = await request.json()

    if not all([message_id, timestamp, received_signature, payload]):
        raise HTTPException(status_code=400, detail="Missing required headers or payload")

    if SHARED_KEY and not verify_signature(message_id or "", timestamp or "", payload_json, received_signature or ""):
        logger.warning(
            "Signature mismatch — id=%s ts=%s sig=%s payload_len=%d",
            message_id,
            timestamp,
            received_signature,
            len(payload_json),
        )
        raise HTTPException(status_code=403, detail="Invalid webhook signature")

    data = payload.get("data", {})
    event_type = payload.get("event", "unknown")
    pc_name = data.get("pc_name", "unknown")
    pc_state = data.get("pc_state", "unknown")
    pc_id = data.get("node_id", "")
    branch = data.get("branch") or "main"

    is_revoke = event_type == "infrahub.proposed_change.approval_revoked"
    pc_decision = data.get("reviewer_former_decision" if is_revoke else "pc_reviewer_decision", "unknown")
    reviewer_name = data.get("reviewer_account_name", "")

    devices_by_type = await fetch_changed_devices(pc_name)
    total = sum(len(v) for v in devices_by_type.values())

    deploy_lines = [
        f"• {dtype} ({len(names)}): {', '.join(sorted(names)[:5])}{'…' if len(names) > 5 else ''}"
        for dtype, names in sorted(devices_by_type.items())
    ]
    device_list = "\n".join(deploy_lines) if deploy_lines else "_No changed device configs_"

    if is_revoke:
        action_text = f"Approval revoked — rollback {total} devices to `main` config"
        device_header = f"Devices to rollback to main ({total})"
        revoked_by = f" by `{reviewer_name}`" if reviewer_name else ""
        comment = (
            f"### Deployment rollback triggered\n\n"
            f"Approval revoked{revoked_by} — rolling back **{total} devices** to `main` config.\n\n"
            f"**Affected devices:**\n{device_list}"
        )
    else:
        action_text = f"Proposed Change {pc_name} — {pc_decision} ({total} devices to deploy)"
        device_header = f"Devices to deploy ({total})"
        comment = (
            f"### Deployment triggered\n\n"
            f"Approved — deploying **{total} devices** with new configuration.\n\n"
            f"**Devices to deploy:**\n{device_list}"
        )

    thread_label = "Devices Rolled Back" if is_revoke else "Devices Deployed"
    await post_thread(pc_id, thread_label, comment)

    result = {
        "text": action_text,
        "blocks": [
            {
                "type": "section",
                "text": {
                    "type": "mrkdwn",
                    "text": (
                        f"*Proposed Change:* `{pc_name}`\n"
                        f"*Decision:* {pc_decision}\n"
                        f"*State:* {pc_state}\n"
                        f"*Branch:* {branch}\n"
                        f"*Event:* `{event_type}`"
                    ),
                },
            },
            {
                "type": "section",
                "text": {"type": "mrkdwn", "text": f"*{device_header}:*\n{device_list}"},
            },
        ],
        "deploy": {
            "action": "rollback" if is_revoke else "deploy",
            "total": total,
            "by_type": {k: sorted(v) for k, v in devices_by_type.items()},
        },
    }

    logger.info("Received webhook: pc=%s event=%s total_devices=%d", pc_name, event_type, total)
    return result


if __name__ == "__main__":
    uvicorn.run(app, host="0.0.0.0", port=8080)
