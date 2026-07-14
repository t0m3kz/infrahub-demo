"""Crossplane → Infrahub approval bridge.

Lifecycle:
  POST /xr/sync     — called when a Crossplane XR is created/updated with
                      annotation infrahub.io/approval-gate: "true".
                      Creates an Infrahub branch, upserts inventory nodes,
                      opens a Proposed Change, and patches the XR annotation
                      with infrahub.io/pc-url so the operator can see it.

  POST /pc/approved — called by an Infrahub webhook when a Proposed Change
                      is merged.  Patches infrahub.io/approval: approved on
                      the XR so the Crossplane composition unblocks.

  POST /xr/ready    — called when the XR reaches Ready=True.
                      Merges the Infrahub branch to main, writes
                      provisioning_status: active, and optionally notifies
                      ServiceNow.

Environment variables
---------------------
INFRAHUB_URL          http://infrahub-server:8000
INFRAHUB_API_TOKEN    <token>
K8S_API_URL           https://kubernetes.default.svc  (in-cluster default)
K8S_TOKEN             <service-account token>          (mounted at well-known path)
K8S_CA_CERT           /var/run/secrets/kubernetes.io/serviceaccount/ca.crt
SERVICENOW_URL        https://instance.service-now.com  (optional)
SERVICENOW_USER       (optional)
SERVICENOW_PASS       (optional)
"""

from __future__ import annotations

import logging
import os

import httpx
import uvicorn
from fastapi import FastAPI, HTTPException, Request
from pydantic import BaseModel

logger = logging.getLogger("crossplane-bridge")
logging.basicConfig(level=logging.INFO, format="%(asctime)s %(levelname)s %(message)s")

app = FastAPI(title="Crossplane-Infrahub Bridge")

INFRAHUB_URL = os.getenv("INFRAHUB_URL", "http://infrahub-server:8000")
INFRAHUB_TOKEN = os.getenv("INFRAHUB_API_TOKEN", "")
K8S_API_URL = os.getenv("K8S_API_URL", "https://kubernetes.default.svc")
K8S_TOKEN_FILE = "/var/run/secrets/kubernetes.io/serviceaccount/token"
K8S_TOKEN = os.getenv("K8S_TOKEN", "")
K8S_CA_CERT = os.getenv("K8S_CA_CERT", "/var/run/secrets/kubernetes.io/serviceaccount/ca.crt")
SNOW_URL = os.getenv("SERVICENOW_URL", "")
SNOW_USER = os.getenv("SERVICENOW_USER", "")
SNOW_PASS = os.getenv("SERVICENOW_PASS", "")

# ── GraphQL mutations / queries ───────────────────────────────────────────────

_CREATE_PC = """
mutation CreatePC($name: String!, $source: String!, $destination: String!, $description: String!) {
  CoreProposedChangeCreate(data: {
    name:        { value: $name }
    source_branch: { value: $source }
    destination_branch: { value: $destination }
    description: { value: $description }
  }) {
    object { id }
  }
}
"""

_UPSERT_TENANT = """
mutation UpsertTenant($name: String!, $si_id: String!, $oe_name: String!, $debtor: String!) {
  OrganizationTenantUpsert(data: {
    name:               { value: $name }
    service_instance_id: { value: $si_id }
    oe_name:            { value: $oe_name }
    debtor:             { value: $debtor }
  }) {
    object { id }
  }
}
"""

_UPSERT_VIRT_CLUSTER = """
mutation UpsertVirtCluster(
  $name: String!, $dns_name: String!, $git_url: String!, $stage: String!, $status: String!
) {
  VirtClusterUpsert(data: {
    name:     { value: $name }
    dns_name: { value: $dns_name }
    git_url:  { value: $git_url }
    stage:    { value: $stage }
    status:   { value: $status }
  }) {
    object { id }
  }
}
"""

_UPSERT_PROXY = """
mutation UpsertProxy(
  $name: String!, $region: String!, $stage: String!,
  $allowed_domains: [String!]!, $network_zone: String!, $status: String!
) {
  ManagedCloudProxyHAUpsert(data: {
    name:            { value: $name }
    region:          { value: $region }
    stage:           { value: $stage }
    allowed_domains: { value: $allowed_domains }
    network_zone:    { value: $network_zone }
    status:          { value: $status }
  }) {
    object { id }
  }
}
"""

_UPSERT_REGISTRATION = """
mutation UpsertRegistration(
  $name: String!, $region: String!, $stage: String!,
  $network_zone: String!, $cloud_provider: String!, $ou: String!, $repository: String!, $status: String!
) {
  AppDeploymentUpsert(data: {
    name:           { value: $name }
    region:         { value: $region }
    stage:          { value: $stage }
    network_zone:   { value: $network_zone }
    cloud_provider: { value: $cloud_provider }
    ou:             { value: $ou }
    repository:     { value: $repository }
    status:         { value: $status }
  }) {
    object { id }
  }
}
"""

_UPDATE_PROVISIONING_STATUS = """
mutation UpdateStatus($id: String!, $status: String!) {
  VirtClusterUpdate(data: { id: $id, status: { value: $status } }) {
    ok
  }
}
"""


# ── Helpers ───────────────────────────────────────────────────────────────────


def _k8s_token() -> str:
    if K8S_TOKEN:
        return K8S_TOKEN
    try:
        with open(K8S_TOKEN_FILE) as f:
            return f.read().strip()
    except OSError:
        return ""


def _k8s_headers() -> dict[str, str]:
    return {"Authorization": f"Bearer {_k8s_token()}"}


def _infrahub_headers() -> dict[str, str]:
    return {"X-INFRAHUB-KEY": INFRAHUB_TOKEN, "Content-Type": "application/json"}


async def _gql(client: httpx.AsyncClient, mutation: str, variables: dict) -> dict:
    resp = await client.post(
        f"{INFRAHUB_URL}/graphql",
        headers=_infrahub_headers(),
        json={"query": mutation, "variables": variables},
        timeout=30,
    )
    resp.raise_for_status()
    data = resp.json()
    if "errors" in data:
        raise RuntimeError(f"GraphQL errors: {data['errors']}")
    return data


async def _create_branch(client: httpx.AsyncClient, branch_name: str) -> None:
    resp = await client.post(
        f"{INFRAHUB_URL}/api/branch",
        headers=_infrahub_headers(),
        json={"name": branch_name, "description": "Crossplane approval gate", "sync_with_git": False},
        timeout=30,
    )
    if resp.status_code not in (200, 201, 409):  # 409 = already exists
        resp.raise_for_status()


async def _get_pc_state(client: httpx.AsyncClient, pc_id: str) -> str:
    query = """
    query PCState($id: String!) {
      CoreProposedChange(ids: [$id]) {
        edges { node { state { value } } }
      }
    }
    """
    data = await _gql(client, query, {"id": pc_id})
    edges = data.get("data", {}).get("CoreProposedChange", {}).get("edges", [])
    if not edges:
        return "unknown"
    return edges[0]["node"]["state"]["value"]


async def _merge_branch(client: httpx.AsyncClient, branch_name: str) -> None:
    resp = await client.post(
        f"{INFRAHUB_URL}/api/branch/{branch_name}/merge",
        headers=_infrahub_headers(),
        timeout=30,
    )
    resp.raise_for_status()


async def _patch_xr_annotation(
    namespace: str, group: str, version: str, plural: str, name: str, annotations: dict[str, str]
) -> None:
    token = _k8s_token()
    if not token:
        logger.warning("No K8s token — skipping XR annotation patch")
        return
    url = f"{K8S_API_URL}/apis/{group}/{version}/namespaces/{namespace}/{plural}/{name}"
    patch = {"metadata": {"annotations": annotations}}
    verify: str | bool = K8S_CA_CERT if os.path.exists(K8S_CA_CERT) else False
    async with httpx.AsyncClient(verify=verify) as k8s:
        resp = await k8s.patch(
            url,
            headers={**_k8s_headers(), "Content-Type": "application/merge-patch+json"},
            json=patch,
            timeout=15,
        )
        if resp.status_code >= 400:
            logger.error("K8s patch failed %d: %s", resp.status_code, resp.text)
        else:
            logger.info("Patched annotations on %s/%s: %s", plural, name, list(annotations.keys()))


async def _notify_servicenow(si_id: str, state: str, message: str) -> None:
    if not SNOW_URL or not si_id:
        return
    try:
        async with httpx.AsyncClient() as snow:
            resp = await snow.post(
                f"{SNOW_URL}/api/now/table/sc_req_item",
                auth=(SNOW_USER, SNOW_PASS),
                json={
                    "correlation_id": si_id,
                    "state": state,
                    "work_notes": message,
                },
                timeout=15,
            )
            if resp.status_code >= 400:
                logger.warning("ServiceNow notify failed %d", resp.status_code)
            else:
                logger.info("ServiceNow updated for SI=%s state=%s", si_id, state)
    except Exception as exc:
        logger.warning("ServiceNow error: %s", exc)


# ── Request models ────────────────────────────────────────────────────────────


class XRRef(BaseModel):
    """Kubernetes XR identity — how to patch it back."""

    group: str
    version: str
    plural: str
    namespace: str
    name: str


class ProfileSpec(BaseModel):
    service_instance_id: str = ""
    oe_name: str = ""
    debtor: str = ""
    cost_center: str = ""
    owners: list[str] = []


class ClusterSpec(BaseModel):
    name: str
    ingress_domain: str = ""
    git_url: str = ""
    stage: str = "dev"


class ProxySpec(BaseModel):
    region: str
    stage: str
    allowed_domains: list[str] = []
    allowed_cidrs: list[str] = []
    network_zone: str = "internet"
    profile: str = ""


class RegistrationSpec(BaseModel):
    repository: str
    network_zone: str
    cloud_provider: str
    region: str
    stage: str
    ou: str
    profile: str = ""


class SyncRequest(BaseModel):
    """Payload sent by the Crossplane function / operator when a gated XR is created/updated."""

    xr: XRRef
    kind: str  # Proxy | Registration | Cluster | Profile
    profile: ProfileSpec = ProfileSpec()
    cluster: ClusterSpec | None = None
    proxy: ProxySpec | None = None
    registration: RegistrationSpec | None = None
    service_instance_id: str = ""  # for ServiceNow correlation


class PCApprovedRequest(BaseModel):
    """Payload from the Infrahub PC-merged webhook."""

    pc_id: str
    pc_name: str
    branch_name: str
    # Bridge stores XR ref in PC description — parsed back here
    xr_group: str = ""
    xr_version: str = ""
    xr_plural: str = ""
    xr_namespace: str = ""
    xr_name: str = ""


class XRReadyRequest(BaseModel):
    """Payload when XR reaches Ready=True."""

    xr: XRRef
    pc_id: str
    branch_name: str
    service_instance_id: str = ""
    provisioned_host: str = ""  # e.g. vcluster host DNS
    infrahub_node_id: str = ""  # VirtCluster / AppDeployment node id in Infrahub


# ── Endpoints ─────────────────────────────────────────────────────────────────


@app.get("/health")
async def health() -> dict:
    return {"status": "ok"}


@app.post("/xr/sync")
async def xr_sync(req: SyncRequest) -> dict:
    """
    Called when a Crossplane XR with infrahub.io/approval-gate: "true" is created or updated.
    1. Creates an Infrahub branch.
    2. Upserts relevant inventory nodes on that branch.
    3. Opens a Proposed Change (destination: main).
    4. Patches the XR with the PC URL so the operator can see it.
    """
    xr = req.xr
    si_id = req.service_instance_id or req.profile.service_instance_id
    branch_name = f"cp/{req.kind.lower()}-{xr.name}"
    pc_name = f"cp-{req.kind.lower()}-{xr.name}"
    pc_description = (
        f"Crossplane approval gate | kind={req.kind} xr={xr.namespace}/{xr.name} "
        f"group={xr.group}/{xr.version}/{xr.plural}"
    )

    async with httpx.AsyncClient() as client:
        # 1. Create branch (idempotent — 409 is fine)
        await _create_branch(client, branch_name)
        logger.info("Branch ready: %s", branch_name)

        # 2. Upsert inventory nodes on the branch
        async def gql_on_branch(mutation: str, variables: dict) -> dict:
            resp = await client.post(
                f"{INFRAHUB_URL}/graphql/{branch_name}",
                headers=_infrahub_headers(),
                json={"query": mutation, "variables": variables},
                timeout=30,
            )
            resp.raise_for_status()
            data = resp.json()
            if "errors" in data:
                raise RuntimeError(f"GraphQL errors on branch: {data['errors']}")
            return data

        # Always upsert the tenant/profile
        if req.profile.oe_name or req.profile.service_instance_id:
            await gql_on_branch(
                _UPSERT_TENANT,
                {
                    "name": xr.name,
                    "si_id": req.profile.service_instance_id,
                    "oe_name": req.profile.oe_name,
                    "debtor": req.profile.debtor,
                },
            )
            logger.info("Upserted OrganizationTenant: %s", xr.name)

        if req.kind == "Cluster" and req.cluster:
            await gql_on_branch(
                _UPSERT_VIRT_CLUSTER,
                {
                    "name": req.cluster.name,
                    "dns_name": req.cluster.ingress_domain,
                    "git_url": req.cluster.git_url,
                    "stage": req.cluster.stage,
                    "status": "planned",
                },
            )
            logger.info("Upserted VirtCluster: %s", req.cluster.name)

        elif req.kind == "Proxy" and req.proxy:
            await gql_on_branch(
                _UPSERT_PROXY,
                {
                    "name": f"cp-proxy-{xr.name}",
                    "region": req.proxy.region,
                    "stage": req.proxy.stage,
                    "allowed_domains": req.proxy.allowed_domains,
                    "network_zone": req.proxy.network_zone,
                    "status": "planned",
                },
            )
            logger.info("Upserted proxy record for %s", xr.name)

        elif req.kind == "Registration" and req.registration:
            await gql_on_branch(
                _UPSERT_REGISTRATION,
                {
                    "name": f"cp-ingress-{xr.name}",
                    "region": req.registration.region,
                    "stage": req.registration.stage,
                    "network_zone": req.registration.network_zone,
                    "cloud_provider": req.registration.cloud_provider,
                    "ou": req.registration.ou,
                    "repository": req.registration.repository,
                    "status": "planned",
                },
            )
            logger.info("Upserted Registration/AppDeployment for %s", xr.name)

        # 3. Open Proposed Change
        pc_data = await _gql(
            client,
            _CREATE_PC,
            {
                "name": pc_name,
                "source": branch_name,
                "destination": "main",
                "description": pc_description,
            },
        )
        pc_id = pc_data["data"]["CoreProposedChangeCreate"]["object"]["id"]
        pc_url = f"{INFRAHUB_URL}/proposed-changes/{pc_id}"
        logger.info("Created PC %s → %s", pc_name, pc_url)

        # 4. Patch XR with PC reference
        await _patch_xr_annotation(
            namespace=xr.namespace,
            group=xr.group,
            version=xr.version,
            plural=xr.plural,
            name=xr.name,
            annotations={
                "infrahub.io/pc-id": pc_id,
                "infrahub.io/pc-url": pc_url,
                "infrahub.io/branch": branch_name,
                "infrahub.io/approval": "pending",
            },
        )

    await _notify_servicenow(si_id, "pending", f"Network approval requested. Review at: {pc_url}")

    return {"pc_id": pc_id, "pc_url": pc_url, "branch": branch_name}


@app.post("/pc/approved")
async def pc_approved(req: PCApprovedRequest) -> dict:
    """
    Called by the Infrahub webhook when a Proposed Change transitions to merged/approved.
    Patches infrahub.io/approval: approved on the XR to unblock the Crossplane composition.
    """
    if not all([req.xr_group, req.xr_version, req.xr_plural, req.xr_namespace, req.xr_name]):
        raise HTTPException(
            status_code=400,
            detail="XR reference fields required (xr_group, xr_version, xr_plural, xr_namespace, xr_name)",
        )

    await _patch_xr_annotation(
        namespace=req.xr_namespace,
        group=req.xr_group,
        version=req.xr_version,
        plural=req.xr_plural,
        name=req.xr_name,
        annotations={"infrahub.io/approval": "approved"},
    )
    logger.info("Patched approval=approved on %s/%s", req.xr_plural, req.xr_name)
    return {"status": "approved", "xr": req.xr_name}


@app.post("/xr/ready")
async def xr_ready(req: XRReadyRequest) -> dict:
    """
    Called when the Crossplane XR reaches Ready=True.
    1. Merges the Infrahub branch to main.
    2. Updates provisioning_status on the Infrahub node.
    3. Notifies ServiceNow.
    """
    async with httpx.AsyncClient() as client:
        # Verify PC is merged before merging branch
        try:
            state = await _get_pc_state(client, req.pc_id)
            if state not in ("merged", "closed"):
                logger.warning("PC %s state is %s — merging branch anyway", req.pc_id, state)
        except Exception as exc:
            logger.warning("Could not verify PC state: %s", exc)

        # Merge branch
        try:
            await _merge_branch(client, req.branch_name)
            logger.info("Merged branch %s to main", req.branch_name)
        except Exception as exc:
            logger.error("Branch merge failed: %s", exc)

        # Update provisioning_status on the Infrahub node
        if req.infrahub_node_id:
            try:
                await _gql(
                    client,
                    _UPDATE_PROVISIONING_STATUS,
                    {
                        "id": req.infrahub_node_id,
                        "status": "active",
                    },
                )
                logger.info("Set provisioning_status=active on node %s", req.infrahub_node_id)
            except Exception as exc:
                logger.warning("Could not update provisioning status: %s", exc)

    message = "Your service is now live."
    if req.provisioned_host:
        message = f"Your service is live at: {req.provisioned_host}"

    await _notify_servicenow(req.service_instance_id, "fulfilled", message)

    await _patch_xr_annotation(
        namespace=req.xr.namespace,
        group=req.xr.group,
        version=req.xr.version,
        plural=req.xr.plural,
        name=req.xr.name,
        annotations={"infrahub.io/provisioning-status": "active"},
    )

    return {"status": "active", "message": message}


# ── Infrahub webhook receiver (PC merged event) ───────────────────────────────


@app.post("/infrahub/webhook")
async def infrahub_webhook(request: Request) -> dict:
    """
    Receives Infrahub webhooks for proposed_change.merged events.
    Parses the XR ref from the PC description and calls the /pc/approved flow.
    """
    payload = await request.json()
    event = payload.get("event", "")
    data = payload.get("data", {})

    if "proposed_change" not in event:
        return {"skipped": True, "reason": "not a proposed change event"}

    state = data.get("pc_state", "")
    if state not in ("merged",):
        return {"skipped": True, "reason": f"state={state}"}

    pc_id = data.get("node_id", "")
    description: str = data.get("description", "")

    # Parse XR ref embedded in description by /xr/sync
    # Format: "... group=<group>/<version>/<plural> xr=<namespace>/<name>"
    xr_group = xr_version = xr_plural = xr_ns = xr_name = ""
    for part in description.split("|"):
        part = part.strip()
        if part.startswith("group="):
            gvp = part[len("group=") :].split("/")
            if len(gvp) == 3:
                xr_group, xr_version, xr_plural = gvp
        elif part.startswith("xr="):
            ns_name = part[len("xr=") :].split("/")
            if len(ns_name) == 2:
                xr_ns, xr_name = ns_name

    if not all([xr_group, xr_version, xr_plural, xr_ns, xr_name]):
        logger.warning("Could not parse XR ref from PC description: %r", description)
        return {"skipped": True, "reason": "xr ref not parseable from description"}

    branch_name: str = data.get("branch") or data.get("source_branch", "")
    pc_name: str = data.get("pc_name", "")

    approved = PCApprovedRequest(
        pc_id=pc_id,
        pc_name=pc_name,
        branch_name=branch_name,
        xr_group=xr_group,
        xr_version=xr_version,
        xr_plural=xr_plural,
        xr_namespace=xr_ns,
        xr_name=xr_name,
    )
    return await pc_approved(approved)


if __name__ == "__main__":
    uvicorn.run(app, host="0.0.0.0", port=8090, log_level="info")
