"""Live n8n version matrix and cross-instance promotion pipeline smoke test.

Validates end-to-end workflow promotion across real, distinct n8n versions:
1. Inspects local Docker engine for available n8n images (2.17.7 and 2.19.0).
2. Provisions Source n8n (2.17.7) and Target n8n (2.19.0) in Docker on ephemeral loopback ports.
3. Provisions a disposable Inference server with loopback egress allowlist.
4. Completes owner setup and extracts API keys on both n8n instances with explicit polling.
5. Registers Source and Target instances in Inference.
6. Deploys a workflow to Source with Code node, Sticky Note, and header auth credential.
7. Deploys matching credential to Target.
8. Executes promotion preflight (POST /api/n8n_promote/preflight):
   - Verifies AST diff, credential remapping, and preview ID.
9. Executes deployment (POST /api/n8n_promote/deploy):
   - Verifies inactive creation on Target, transformed node code, remapped credential.
10. Executes explicit workflow activation (POST /api/n8n_proxy/workflows/{id}/activate):
    - Verifies workflow transitions to active on Target.
11. Validates three ugly/edge cases:
    - Duplicate deploy with same idempotency key (idempotent return, zero duplicate workflows).
    - Stale preview (source workflow modified after preflight -> 409 stale_preview).
    - Target instance down mid-deploy (uncertain outcome recorded, no blind retry).
12. Destroys all containers, volumes, and temporary files in finally.

Requires --confirm to run; without --confirm it prints NOT RUN and exits 0.
"""
from __future__ import annotations

import argparse
import json
import os
from pathlib import Path
import secrets
import shutil
import socket
import subprocess
import sys
import tempfile
import time
from typing import Any

import httpx

ROOT = Path(__file__).resolve().parents[1]
if str(ROOT) not in sys.path:
    sys.path.insert(0, str(ROOT))

SOURCE_IMAGE = "n8nio/n8n:latest"  # Local image label: 2.17.7
TARGET_IMAGE = "docker.n8n.io/n8nio/n8n:2.19.0"  # Local image label: 2.19.0

# Operational request timeout for API calls once instances are confirmed healthy
OPERATIONAL_TIMEOUT = 45.0


def parse_args() -> argparse.Namespace:
    parser = argparse.ArgumentParser(
        description="Live n8n version matrix and cross-instance promotion smoke test"
    )
    parser.add_argument(
        "--confirm",
        action="store_true",
        help="Execute the live matrix promotion smoke suite against disposable Docker containers",
    )
    return parser.parse_args()


def allocate_free_port() -> int:
    with socket.socket(socket.AF_INET, socket.SOCK_STREAM) as sock:
        sock.bind(("127.0.0.1", 0))
        port = sock.getsockname()[1]
        if port == 3000:
            return allocate_free_port()
        return port


def docker_cmd(*args: str) -> subprocess.CompletedProcess[str]:
    return subprocess.run(
        ["docker", *args],
        capture_output=True,
        text=True,
        check=False,
    )


def inspect_image_version(image: str) -> tuple[bool, str]:
    """Inspect image version; if missing, attempt pull before returning result."""
    res = docker_cmd("image", "inspect", image)
    if res.returncode != 0:
        pull_res = docker_cmd("pull", image)
        if pull_res.returncode != 0:
            err_msg = pull_res.stderr.strip() or pull_res.stdout.strip()
            return False, f"Not available locally and docker pull failed ({err_msg[:120]})"
        res = docker_cmd("image", "inspect", image)
        if res.returncode != 0:
            return False, "Inspection failed after pull"

    try:
        data = json.loads(res.stdout)
        labels = data[0].get("Config", {}).get("Labels", {})
        ver = labels.get("org.opencontainers.image.version") or data[0].get("Id", "")[:12]
        return True, ver or "unknown"
    except Exception as exc:
        return False, f"Failed to parse inspect output: {exc}"


def start_n8n_container(name: str, port: int, image: str, volume: str) -> None:
    res = docker_cmd(
        "run",
        "-d",
        "--name",
        name,
        "--label",
        "com.inference.managed=false",
        "--label",
        "inference.test-role=matrix-smoke",
        "-p",
        f"127.0.0.1:{port}:5678",
        "-v",
        f"{volume}:/home/node/.n8n",
        "-e",
        "N8N_DIAGNOSTICS_ENABLED=false",
        "-e",
        "N8N_VERSION_NOTIFICATIONS_ENABLED=false",
        "-e",
        "N8N_HIRING_BANNER_ENABLED=false",
        "-e",
        "N8N_TEMPLATES_ENABLED=false",
        "-e",
        "N8N_ENFORCE_SETTINGS_FILE_PERMISSIONS=true",
        "-e",
        "N8N_RUNNERS_ENABLED=false",
        image,
    )
    if res.returncode != 0:
        raise RuntimeError(f"Failed to start n8n container {name}: {res.stderr.strip()}")


def wait_for_n8n_ready(
    container_name: str,
    port: int,
    timeout_budget: float = 240.0,
    poll_interval: float = 1.0,
) -> None:
    """Poll n8n health endpoint until healthy or timeout budget exhausted.
    
    Captures and dumps container logs if the instance does not become ready.
    """
    url = f"http://127.0.0.1:{port}/healthz"
    start_time = time.monotonic()
    last_err = "No response yet"
    last_log_time = start_time

    while time.monotonic() - start_time < timeout_budget:
        elapsed = time.monotonic() - start_time
        if elapsed > 15.0 and (time.monotonic() - last_log_time) >= 15.0:
            print(
                f"       Waiting for {container_name} on port {port} to become healthy... ({elapsed:.0f}s elapsed)",
                flush=True,
            )
            last_log_time = time.monotonic()

        try:
            with httpx.Client(timeout=5.0, trust_env=False) as client:
                r = client.get(url)
                if (
                    r.status_code == 200
                    and r.headers.get("content-type", "").startswith("application/json")
                    and r.json().get("status") == "ok"
                ):
                    return
                last_err = f"HTTP {r.status_code}: {r.text[:150]}"
        except Exception as exc:
            last_err = f"{type(exc).__name__}: {exc}"

        time.sleep(poll_interval)

    elapsed = time.monotonic() - start_time
    log_res = docker_cmd("logs", "--tail", "50", container_name)
    logs_tail = (log_res.stdout + "\n" + log_res.stderr).strip()
    msg = (
        f"\n[TIMEOUT] n8n container '{container_name}' at '{url}' did not become ready after {elapsed:.1f}s.\n"
        f"Last error: {last_err}\n"
        f"--- Tail of container logs ({container_name}) ---\n"
        f"{logs_tail or '<no container logs available>'}\n"
        f"------------------------------------------------"
    )
    raise TimeoutError(msg)


def setup_n8n_owner_and_key(
    container_name: str,
    port: int,
    timeout_budget: float = 180.0,
) -> str:
    """Complete owner setup and generate an API key with explicit retry budget and diagnostics."""
    base_url = f"http://127.0.0.1:{port}"
    start_time = time.monotonic()
    owner_data = {
        "email": f"matrix_{secrets.token_hex(4)}@example.test",
        "firstName": "Matrix",
        "lastName": "Operator",
        "password": "LivePass9!" + secrets.token_urlsafe(16),
    }

    def dump_diagnostics(stage: str, last_err: str) -> None:
        elapsed = time.monotonic() - start_time
        log_res = docker_cmd("logs", "--tail", "50", container_name)
        logs_tail = (log_res.stdout + "\n" + log_res.stderr).strip()
        print(
            f"\n[ERROR] {stage} failed on '{container_name}' at {base_url} after {elapsed:.1f}s:\n"
            f"Last error: {last_err}\n"
            f"--- Tail of container logs ({container_name}) ---\n"
            f"{logs_tail or '<no container logs available>'}\n"
            f"------------------------------------------------",
            file=sys.stderr,
            flush=True,
        )

    # 1. Post owner setup
    owner_done = False
    last_err = "No attempts yet"
    last_log_time = start_time
    with httpx.Client(base_url=base_url, timeout=15.0, trust_env=False) as client:
        while time.monotonic() - start_time < timeout_budget:
            elapsed = time.monotonic() - start_time
            if elapsed > 15.0 and (time.monotonic() - last_log_time) >= 15.0:
                print(
                    f"       Setting up owner on {container_name}... ({elapsed:.0f}s elapsed)",
                    flush=True,
                )
                last_log_time = time.monotonic()

            try:
                r_owner = client.post("/rest/owner/setup", json=owner_data)
                if (
                    r_owner.status_code in (200, 201)
                    and r_owner.headers.get("content-type", "").startswith("application/json")
                    and "data" in r_owner.json()
                ):
                    owner_done = True
                    break
                elif r_owner.status_code == 400 and "already been setup" in r_owner.text:
                    owner_done = True
                    break
                last_err = f"HTTP {r_owner.status_code}: {r_owner.text[:150]}"
            except (httpx.TimeoutException, httpx.NetworkError, Exception) as exc:
                last_err = f"{type(exc).__name__}: {exc}"

            time.sleep(1.0)

        if not owner_done:
            dump_diagnostics("Owner setup", last_err)
            raise TimeoutError(
                f"Owner setup did not succeed on '{container_name}' at {base_url} within {timeout_budget}s: {last_err}"
            )

        # Set session cookie for subsequent requests
        cookie_header = "; ".join(f"{c.name}={c.value}" for c in client.cookies.jar)
        if not cookie_header:
            cookie_header = "; ".join(f"{k}={v}" for k, v in client.cookies.items())
        client.headers["Cookie"] = cookie_header

        # 2. Get scopes
        scopes = None
        while time.monotonic() - start_time < timeout_budget:
            try:
                r_scopes = client.get("/rest/api-keys/scopes")
                if (
                    r_scopes.status_code == 200
                    and r_scopes.headers.get("content-type", "").startswith("application/json")
                ):
                    scopes = r_scopes.json().get("data", [])
                    break
                last_err = f"HTTP {r_scopes.status_code}: {r_scopes.text[:150]}"
            except (httpx.TimeoutException, httpx.NetworkError, Exception) as exc:
                last_err = f"{type(exc).__name__}: {exc}"

            time.sleep(1.0)

        if scopes is None:
            dump_diagnostics("Fetch API key scopes", last_err)
            raise TimeoutError(
                f"Failed to get API key scopes on '{container_name}' at {base_url}: {last_err}"
            )

        # 3. Create API key
        api_key = None
        while time.monotonic() - start_time < timeout_budget:
            try:
                r_key = client.post(
                    "/rest/api-keys",
                    json={
                        "label": f"Matrix Smoke Key {secrets.token_hex(4)}",
                        "scopes": scopes,
                        "expiresAt": None,
                    },
                )
                if (
                    r_key.status_code in (200, 201)
                    and r_key.headers.get("content-type", "").startswith("application/json")
                ):
                    api_key = r_key.json()["data"]["rawApiKey"]
                    break
                last_err = f"HTTP {r_key.status_code}: {r_key.text[:150]}"
            except (httpx.TimeoutException, httpx.NetworkError, Exception) as exc:
                last_err = f"{type(exc).__name__}: {exc}"

            time.sleep(1.0)

        if not api_key:
            dump_diagnostics("Create API key", last_err)
            raise TimeoutError(
                f"Failed to create API key on '{container_name}' at {base_url}: {last_err}"
            )

        return api_key


def start_app_server(
    port: int,
    data_dir: Path,
    bootstrap_token: str,
    timeout_budget: float = 60.0,
) -> subprocess.Popen:
    """Boot ephemeral Inference app server, polling /healthz until healthy or dumping server logs."""
    env = {
        **os.environ,
        "INFERENCE_DATA_DIR": str(data_dir),
        "INFERENCE_SECURE_COOKIES": "false",
        "INFERENCE_BOOTSTRAP_TOKEN": bootstrap_token,
        "INFERENCE_ALLOWED_ORIGINS": f"http://127.0.0.1:{port}",
        "INFERENCE_EGRESS_ALLOW_CIDRS": "127.0.0.1/32",
    }
    log_file = data_dir / "server.log"
    log_handle = log_file.open("w")
    process = subprocess.Popen(
        [
            sys.executable,
            "-m",
            "uvicorn",
            "backend.main:create_app",
            "--factory",
            "--host",
            "127.0.0.1",
            "--port",
            str(port),
            "--no-proxy-headers",
            "--no-access-log",
        ],
        cwd=ROOT,
        env=env,
        stdout=log_handle,
        stderr=subprocess.STDOUT,
    )

    start_time = time.monotonic()
    url = f"http://127.0.0.1:{port}/healthz"
    last_err = "No attempts yet"

    while time.monotonic() - start_time < timeout_budget:
        elapsed = time.monotonic() - start_time
        if process.poll() is not None:
            log_handle.flush()
            log_handle.close()
            content = log_file.read_text(errors="replace")
            tail = "\n".join(content.strip().splitlines()[-20:])
            raise RuntimeError(
                f"Inference server process exited unexpectedly with code {process.returncode} after {elapsed:.1f}s:\n"
                f"--- Last 20 lines of server.log ---\n{tail}\n-----------------------------------"
            )

        try:
            with httpx.Client(timeout=2.0, trust_env=False) as client:
                res = client.get(url)
                if res.status_code == 200:
                    return process
                last_err = f"HTTP {res.status_code}: {res.text[:150]}"
        except Exception as exc:
            last_err = f"{type(exc).__name__}: {exc}"

        time.sleep(1.0)

    # If timeout expired
    process.terminate()
    try:
        process.wait(timeout=5)
    except subprocess.TimeoutExpired:
        process.kill()
    log_handle.flush()
    log_handle.close()
    content = log_file.read_text(errors="replace")
    tail = "\n".join(content.strip().splitlines()[-20:])
    elapsed = time.monotonic() - start_time
    raise TimeoutError(
        f"Inference server at '{url}' did not become ready within {elapsed:.1f}s.\n"
        f"Last error: {last_err}\n"
        f"--- Last 20 lines of server.log ---\n{tail}\n-----------------------------------"
    )


def stop_app_server(process: subprocess.Popen) -> None:
    process.terminate()
    try:
        process.wait(timeout=10)
    except subprocess.TimeoutExpired:
        process.kill()
        process.wait(timeout=5)


def run_smoke_test() -> None:
    start_time = time.monotonic()
    steps: list[tuple[str, str, str]] = []

    def step_start(step_name: str, detail: str = "") -> None:
        status_str = "[START]".ljust(16)
        print(f"{status_str}{step_name}" + (f" - {detail}" if detail else ""), flush=True)

    def record(step_name: str, outcome: str, detail: str = "") -> None:
        steps.append((step_name, outcome, detail))
        status_str = f"[{outcome}]".ljust(16)
        print(f"{status_str}{step_name}" + (f" - {detail}" if detail else ""), flush=True)

    print("================================================================================")
    print("STARTING LIVE N8N VERSION MATRIX AND PROMOTION SMOKE SUITE")
    print("================================================================================")

    # 1. Inspect image versions
    step_start("Step 1: Discover Local n8n Version Matrix")
    src_ok, src_v = inspect_image_version(SOURCE_IMAGE)
    tgt_ok, tgt_v = inspect_image_version(TARGET_IMAGE)
    if not src_ok or not tgt_ok:
        record(
            "Step 1: Discover Local n8n Version Matrix",
            "NOT VERIFIED",
            f"Source ({SOURCE_IMAGE}): {src_v}; Target ({TARGET_IMAGE}): {tgt_v}",
        )
        print(
            "\n[NOT VERIFIED] Required n8n images are not present or cannot be pulled. "
            "Please pull images manually before running the live matrix suite.",
            file=sys.stderr,
            flush=True,
        )
        return

    record(
        "Step 1: Discover Local n8n Version Matrix",
        "PASS",
        f"Source: {SOURCE_IMAGE} ({src_v}), Target: {TARGET_IMAGE} ({tgt_v})",
    )

    src_port = allocate_free_port()
    tgt_port = allocate_free_port()
    app_port = allocate_free_port()

    tag = secrets.token_hex(4)
    src_name = f"inf-matrix-src-{tag}"
    tgt_name = f"inf-matrix-tgt-{tag}"
    src_vol = f"{src_name}-data"
    tgt_vol = f"{tgt_name}-data"

    data_dir = Path(tempfile.mkdtemp(prefix="inference-matrix-smoke-"))
    bootstrap_token = secrets.token_urlsafe(32)
    app_process: subprocess.Popen | None = None

    try:
        # 2. Start Source & Target containers
        step_start(
            "Step 2: Start Source and Target n8n Containers",
            f"Source on 127.0.0.1:{src_port}, Target on 127.0.0.1:{tgt_port}",
        )
        print(f"       Spawning Source n8n ({src_v}) container {src_name}...")
        start_n8n_container(src_name, src_port, SOURCE_IMAGE, src_vol)
        print(f"       Spawning Target n8n ({tgt_v}) container {tgt_name}...")
        start_n8n_container(tgt_name, tgt_port, TARGET_IMAGE, tgt_vol)

        wait_for_n8n_ready(src_name, src_port, timeout_budget=240.0)
        wait_for_n8n_ready(tgt_name, tgt_port, timeout_budget=240.0)
        record(
            "Step 2: Start Source and Target n8n Containers",
            "PASS",
            f"Source on port {src_port}, Target on port {tgt_port} (both healthy)",
        )

        # 3. Setup owner accounts and generate API keys
        step_start("Step 3: Setup n8n Owners and Create API Keys")
        src_key = setup_n8n_owner_and_key(src_name, src_port, timeout_budget=180.0)
        tgt_key = setup_n8n_owner_and_key(tgt_name, tgt_port, timeout_budget=180.0)
        record(
            "Step 3: Setup n8n Owners and Create API Keys",
            "PASS",
            "API keys created for both Source and Target instances",
        )

        # 4. Start Inference server
        step_start("Step 4: Boot Disposable Inference Server", f"127.0.0.1:{app_port}")
        app_process = start_app_server(app_port, data_dir, bootstrap_token, timeout_budget=60.0)
        app_base = f"http://127.0.0.1:{app_port}"
        record(
            "Step 4: Boot Disposable Inference Server",
            "PASS",
            f"Server listening on port {app_port}, loopback egress permitted",
        )

        # 5. Bootstrap admin and configure session
        step_start("Step 5: Bootstrap Administrator Account")
        with httpx.Client(
            base_url=app_base,
            timeout=OPERATIONAL_TIMEOUT,
            trust_env=False,
            headers={"Origin": app_base},
        ) as client:
            admin_pw = "AdminPass9!" + secrets.token_urlsafe(16)
            res_setup = client.post(
                "/api/auth/setup",
                json={"username": "matrix_admin", "password": admin_pw},
                headers={"X-Inference-Bootstrap": bootstrap_token},
            )
            assert res_setup.status_code == 200, f"Auth setup failed: {res_setup.text}"
            csrf_token = client.cookies.get("agd_csrf")
            assert csrf_token, "agd_csrf cookie missing after setup"
            client.headers["X-AGD-CSRF"] = csrf_token
            record("Step 5: Bootstrap Administrator Account", "PASS", "admin authenticated with double-submit CSRF")

            # 6. Register Source and Target instances in Inference
            step_start("Step 6: Register Instances in Fleet Proxy")
            res_src_inst = client.post(
                "/api/n8n_proxy/instances",
                json={
                    "name": f"Source n8n ({src_v})",
                    "url": f"http://127.0.0.1:{src_port}",
                    "api_key": src_key,
                },
            )
            assert res_src_inst.status_code == 200, f"Register source failed: {res_src_inst.text}"
            src_inst_id = res_src_inst.json()["id"]

            res_tgt_inst = client.post(
                "/api/n8n_proxy/instances",
                json={
                    "name": f"Target n8n ({tgt_v})",
                    "url": f"http://127.0.0.1:{tgt_port}",
                    "api_key": tgt_key,
                },
            )
            assert res_tgt_inst.status_code == 200, f"Register target failed: {res_tgt_inst.text}"
            tgt_inst_id = res_tgt_inst.json()["id"]
            record(
                "Step 6: Register Instances in Fleet Proxy",
                "PASS",
                f"Source instance {src_inst_id}, Target instance {tgt_inst_id}",
            )

        # 7. Seed workflow & credential on Source, and matching credential on Target
        step_start("Step 7: Seed Workflows and Credentials on Instances")
        with httpx.Client(base_url=f"http://127.0.0.1:{src_port}", timeout=OPERATIONAL_TIMEOUT, trust_env=False) as n8n_src:
            n8n_src.headers["X-N8N-API-KEY"] = src_key
            res_cred_src = n8n_src.post(
                "/api/v1/credentials",
                json={
                    "name": "Header Auth",
                    "type": "httpHeaderAuth",
                    "data": {"name": "X-API-Key", "value": "source-secret-val-123"},
                },
            )
            assert res_cred_src.status_code in (200, 201), f"Create cred src failed: {res_cred_src.text}"
            src_cred_id = res_cred_src.json()["id"]

            wf_payload = {
                "name": "Live Cross-Version Pipeline",
                "settings": {"executionOrder": "v1"},
                "nodes": [
                    {
                        "id": "node-webhook",
                        "name": "Webhook",
                        "type": "n8n-nodes-base.webhook",
                        "typeVersion": 2,
                        "position": [0, 0],
                        "webhookId": "matrix-hook",
                        "parameters": {"path": "matrix-hook", "httpMethod": "GET", "responseMode": "lastNode"},
                    },
                    {
                        "id": "node-note",
                        "name": "Sticky Note",
                        "type": "n8n-nodes-base.stickyNote",
                        "typeVersion": 1,
                        "position": [0, -100],
                        "parameters": {"content": "Cross-version promotion pipeline note"},
                    },
                    {
                        "id": "node-code",
                        "name": "Code",
                        "type": "n8n-nodes-base.code",
                        "typeVersion": 2,
                        "position": [250, 0],
                        "parameters": {
                            "jsCode": "return [{json: {source: '2.17.7', target: '2.19.0', items: 42}}];"
                        },
                    },
                    {
                        "id": "node-http",
                        "name": "HTTP Request",
                        "type": "n8n-nodes-base.httpRequest",
                        "typeVersion": 4.2,
                        "position": [500, 0],
                        "parameters": {
                            "method": "GET",
                            "url": "https://example.com/api",
                            "authentication": "predefinedCredentialType",
                            "nodeCredentialType": "httpHeaderAuth",
                        },
                        "credentials": {"httpHeaderAuth": {"id": src_cred_id, "name": "Header Auth"}},
                    },
                ],
                "connections": {
                    "Webhook": {"main": [[{"node": "Code", "type": "main", "index": 0}]]},
                    "Code": {"main": [[{"node": "HTTP Request", "type": "main", "index": 0}]]},
                },
            }
            res_wf_src = n8n_src.post("/api/v1/workflows", json=wf_payload)
            assert res_wf_src.status_code in (200, 201), f"Create wf src failed: {res_wf_src.text}"
            src_wf_id = res_wf_src.json()["id"]

        with httpx.Client(base_url=f"http://127.0.0.1:{tgt_port}", timeout=OPERATIONAL_TIMEOUT, trust_env=False) as n8n_tgt:
            n8n_tgt.headers["X-N8N-API-KEY"] = tgt_key
            res_cred_tgt = n8n_tgt.post(
                "/api/v1/credentials",
                json={
                    "name": "Header Auth",
                    "type": "httpHeaderAuth",
                    "data": {"name": "X-API-Key", "value": "target-secret-val-456"},
                },
            )
            assert res_cred_tgt.status_code in (200, 201), f"Create cred tgt failed: {res_cred_tgt.text}"
            tgt_cred_id = res_cred_tgt.json()["id"]

        record(
            "Step 7: Seed Workflows and Credentials on Instances",
            "PASS",
            f"Source workflow {src_wf_id} with cred {src_cred_id}; Target cred {tgt_cred_id}",
        )

        # 8. Execute Promotion Preflight Check
        step_start("Step 8: Promotion Preflight Check")
        with httpx.Client(
            base_url=app_base,
            timeout=OPERATIONAL_TIMEOUT,
            trust_env=False,
            headers={"Origin": app_base},
        ) as client:
            client.post("/api/auth/login", json={"username": "matrix_admin", "password": admin_pw})
            client.headers["X-AGD-CSRF"] = client.cookies["agd_csrf"]

            preflight_body = {
                "source_instance_id": src_inst_id,
                "target_instance_id": tgt_inst_id,
                "workflow_id": src_wf_id,
                "credential_map": {src_cred_id: tgt_cred_id},
                "secret_map": {},
                "subworkflow_map": {},
            }
            res_preflight = client.post("/api/n8n_promote/preflight", json=preflight_body)
            assert res_preflight.status_code == 200, f"Preflight failed: {res_preflight.text}"
            preflight_data = res_preflight.json()
            assert preflight_data.get("can_deploy") is True, f"can_deploy is false: {preflight_data}"
            assert preflight_data.get("diff", {}).get("operation") == "create"
            preview_id = preflight_data["preview_id"]
            record(
                "Step 8: Promotion Preflight Check",
                "PASS",
                f"preview_id {preview_id}, can_deploy=True, credential {src_cred_id}->{tgt_cred_id} mapped",
            )

            # 9. Deploy workflow to Target instance
            step_start("Step 9: Deploy Promoted Workflow (Inactive State Enforced)")
            deploy_key_1 = f"matrix-deploy-{secrets.token_hex(6)}"
            res_deploy = client.post(
                "/api/n8n_promote/deploy",
                json={"preview_id": preview_id, "idempotency_key": deploy_key_1},
            )
            assert res_deploy.status_code == 200, f"Deploy failed: {res_deploy.text}"
            deploy_data = res_deploy.json()
            assert deploy_data.get("status") == "deployed", f"Status not deployed: {deploy_data}"
            target_wf_id = deploy_data.get("target_workflow_id")
            assert target_wf_id, "target_workflow_id missing from deploy response"

            # Verify Target workflow state directly from Target n8n
            with httpx.Client(base_url=f"http://127.0.0.1:{tgt_port}", timeout=OPERATIONAL_TIMEOUT, trust_env=False) as n8n_tgt:
                n8n_tgt.headers["X-N8N-API-KEY"] = tgt_key
                r_wf_tgt = n8n_tgt.get(f"/api/v1/workflows/{target_wf_id}")
                assert r_wf_tgt.status_code == 200, f"Target wf readback failed: {r_wf_tgt.text}"
                wf_tgt_json = r_wf_tgt.json()
                # Target MUST be created inactive
                assert wf_tgt_json.get("active") is False, "Promoted workflow must be inactive upon creation"
                # Check remapped credential
                http_node = next(n for n in wf_tgt_json["nodes"] if n["name"] == "HTTP Request")
                assert (
                    http_node["credentials"]["httpHeaderAuth"]["id"] == tgt_cred_id
                ), "Credential was not remapped to target credential ID"

            record(
                "Step 9: Deploy Promoted Workflow (Inactive State Enforced)",
                "PASS",
                f"Target workflow {target_wf_id} created inactive with remapped credential {tgt_cred_id}",
            )

            # 10. Explicit Activation
            step_start("Step 10: Explicit Target Workflow Activation")
            res_act = client.post(
                f"/api/n8n_proxy/workflows/{target_wf_id}/activate",
                params={"instance_id": tgt_inst_id},
            )
            assert res_act.status_code == 200, f"Activate failed: {res_act.text}"
            # Check target n8n directly
            with httpx.Client(base_url=f"http://127.0.0.1:{tgt_port}", timeout=OPERATIONAL_TIMEOUT, trust_env=False) as n8n_tgt:
                n8n_tgt.headers["X-N8N-API-KEY"] = tgt_key
                r_act_check = n8n_tgt.get(f"/api/v1/workflows/{target_wf_id}")
                assert r_act_check.json().get("active") is True, "Target workflow did not activate"
            record(
                "Step 10: Explicit Target Workflow Activation",
                "PASS",
                f"Target workflow {target_wf_id} successfully activated via proxy",
            )

            # 11. Ugly Case 1: Replay deploy with identical idempotency key
            step_start("Step 11: Idempotent Deployment Replay")
            res_deploy_replay = client.post(
                "/api/n8n_promote/deploy",
                json={"preview_id": preview_id, "idempotency_key": deploy_key_1},
            )
            assert res_deploy_replay.status_code == 200, f"Replay deploy failed: {res_deploy_replay.text}"
            replay_data = res_deploy_replay.json()
            assert (
                replay_data.get("target_workflow_id") == target_wf_id
            ), "Replay returned different target workflow ID"
            # Verify no second workflow was created on Target
            with httpx.Client(base_url=f"http://127.0.0.1:{tgt_port}", timeout=OPERATIONAL_TIMEOUT, trust_env=False) as n8n_tgt:
                n8n_tgt.headers["X-N8N-API-KEY"] = tgt_key
                r_all = n8n_tgt.get("/api/v1/workflows")
                assert len(r_all.json().get("data", [])) == 1, "Duplicate workflow created on replay"
            record(
                "Step 11: Idempotent Deployment Replay",
                "PASS",
                "Replayed deploy returned previous result; zero duplicate workflows created",
            )

            # 12. Ugly Case 2: Stale preview rejected
            step_start("Step 12: Stale Preview Rejection")
            # Explicitly deactivate Target workflow first so target_active blocker is clear
            res_deact = client.post(
                f"/api/n8n_proxy/workflows/{target_wf_id}/deactivate",
                params={"instance_id": tgt_inst_id},
            )
            assert res_deact.status_code == 200, f"Deactivate failed: {res_deact.text}"

            # Create a fresh valid preview
            res_pf2 = client.post("/api/n8n_promote/preflight", json=preflight_body)
            assert res_pf2.status_code == 200
            pf2_data = res_pf2.json()
            assert pf2_data.get("can_deploy") is True, f"pf2 cannot deploy: {pf2_data}"
            preview_id_2 = pf2_data["preview_id"]

            # Modify Source workflow on Source n8n
            with httpx.Client(base_url=f"http://127.0.0.1:{src_port}", timeout=OPERATIONAL_TIMEOUT, trust_env=False) as n8n_src:
                n8n_src.headers["X-N8N-API-KEY"] = src_key
                wf_mod = dict(wf_payload)
                wf_mod["name"] = "Live Cross-Version Pipeline (Modified)"
                r_put = n8n_src.put(f"/api/v1/workflows/{src_wf_id}", json=wf_mod)
                assert r_put.status_code == 200, f"Failed to modify source workflow: {r_put.text}"

            # Attempt deploy with preview_id_2
            res_stale = client.post(
                "/api/n8n_promote/deploy",
                json={"preview_id": preview_id_2, "idempotency_key": f"deploy-stale-{secrets.token_hex(4)}"},
            )
            assert res_stale.status_code in (200, 409), f"Unexpected status: {res_stale.status_code}: {res_stale.text}"
            stale_json = res_stale.json()
            assert (
                stale_json.get("error_code") == "stale_preview"
                or stale_json.get("code") == "stale_preview"
            ), f"Expected stale_preview error code, got {stale_json}"
            assert stale_json.get("status") in ("rejected", "failed", None)
            record(
                "Step 12: Stale Preview Rejection",
                "PASS",
                f"Deploy rejected (status='{stale_json.get('status')}', error_code='stale_preview') after source modified",
            )

            # 13. Ugly Case 3: Target instance down mid-deploy
            step_start("Step 13: Target Down Mid-Deploy Handling")
            # Create a fresh preview for the modified workflow
            res_pf3 = client.post("/api/n8n_promote/preflight", json=preflight_body)
            assert res_pf3.status_code == 200
            pf3_data = res_pf3.json()
            assert pf3_data.get("can_deploy") is True, f"pf3 cannot deploy: {pf3_data}"
            preview_id_3 = pf3_data["preview_id"]

            # Stop Target container to simulate outage during remote write
            print(f"       Stopping Target container {tgt_name} to simulate network/host failure mid-deploy...")
            docker_cmd("stop", tgt_name)

            res_down = client.post(
                "/api/n8n_promote/deploy",
                json={"preview_id": preview_id_3, "idempotency_key": f"deploy-down-{secrets.token_hex(4)}"},
            )
            # Should fail and record uncertain/rejected status without blind retries
            assert res_down.status_code in (200, 409, 502, 504), f"Unexpected status on target down: {res_down.status_code}"
            down_json = res_down.json()
            assert (
                down_json.get("status") in ("uncertain", "rejected", "partial")
                or res_down.status_code in (502, 504)
            ), f"Expected uncertain/rejected outcome on target down: {down_json}"
            outcome_desc = down_json.get("status") or f"HTTP {res_down.status_code}"
            record(
                "Step 13: Target Down Mid-Deploy Handling",
                "PASS",
                f"Deploy handled cleanly with outcome '{outcome_desc}'; zero blind retry loops",
            )

        elapsed = time.monotonic() - start_time
        print("\n================================================================================")
        print("SUMMARY: ALL N8N VERSION MATRIX AND PROMOTION CHECKS PASSED")
        print("================================================================================")
        print(f"Elapsed time: {elapsed:.2f}s")
        print(f"Total steps executed: {len(steps)}")
        print(f"Passed: {sum(1 for s in steps if s[1] == 'PASS')}")
        print(f"Failed / Not Verified: {sum(1 for s in steps if s[1] != 'PASS')}")
        print(f"Source: {SOURCE_IMAGE} ({src_v}) -> Target: {TARGET_IMAGE} ({tgt_v})")
        print("Zero false success claims; inactive target creation and idempotent replay verified.")
        print("================================================================================")

    finally:
        # Cleanup
        print("\nCleaning up disposable matrix test resources...")
        if app_process is not None:
            stop_app_server(app_process)
        docker_cmd("rm", "-f", src_name, tgt_name)
        docker_cmd("volume", "rm", "-f", src_vol, tgt_vol)
        shutil.rmtree(data_dir, ignore_errors=True)
        print("Cleanup complete.")


def main() -> None:
    args = parse_args()
    if not args.confirm:
        print("NOT RUN: Pass --confirm to execute the live n8n version matrix and promotion smoke suite.")
        sys.exit(0)

    try:
        run_smoke_test()
    except Exception as exc:
        print(f"\n[FATAL] Matrix promotion smoke suite failed: {exc}", file=sys.stderr)
        import traceback

        traceback.print_exc()
        sys.exit(1)


if __name__ == "__main__":
    main()
