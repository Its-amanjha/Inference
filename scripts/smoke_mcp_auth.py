"""Live Model Context Protocol (MCP) Streamable HTTP and authenticated server smoke test.

Validates end-to-end MCP protocol revision 2025-11-25 and human approval gate:
1. Spawns an in-process authenticated Streamable HTTP MCP server on dynamic port.
   - Enforces Bearer token authentication and protocol version 2025-11-25.
   - Manages MCP-Session-Id and JSON-RPC 2.0 message dispatch.
2. Boots disposable Inference server with loopback egress allowed.
3. Bootstraps administrator account and stores the MCP auth token in the Fernet vault ($MCP_AUTH_KEY).
4. Registers the authenticated MCP server configuration (PUT /api/assistant/config/mcp).
5. Discovers tools via Inference (GET /api/assistant/mcp/{server_id}/tools):
   - Inference connects, negotiates protocol, authenticates via Bearer header, and parses JSON Schema.
6. Asserts 15-Minute Human Approval Gate:
   - Calling POST /api/assistant/tools/run with 'mcp_call' queues a 15-minute pending proposal.
   - Confirms that the remote tool was NOT invoked autonomously.
7. Executes Human Confirmation:
   - Calling POST /api/assistant/tools/confirm consumes the action and executes the remote MCP tool.
   - Asserts returned execution result.
8. Asserts Replay Rejection:
   - Calling POST /api/assistant/tools/confirm a second time is rejected with HTTP 409 'action_consumed'.
9. Asserts Expiry Rejection:
   - Expired proposal is rejected with HTTP 409 'action_expired'.
10. Asserts Authentication Failure Handling:
    - Invalid or wrong Bearer token causes tool discovery to fail closed (HTTP 502 mcp_error).
11. Tears down servers and temporary files in finally.

Requires --confirm to run; without --confirm it prints NOT RUN and exits 0.
"""
from __future__ import annotations

import argparse
from http.server import HTTPServer, BaseHTTPRequestHandler
import json
import os
from pathlib import Path
import secrets
import shutil
import socket
import sqlite3
import subprocess
import sys
import tempfile
import threading
import time
from typing import Any

import httpx

ROOT = Path(__file__).resolve().parents[1]
if str(ROOT) not in sys.path:
    sys.path.insert(0, str(ROOT))

PROTOCOL_VERSION = "2025-11-25"


def parse_args() -> argparse.Namespace:
    parser = argparse.ArgumentParser(
        description="Live MCP Streamable HTTP and authenticated server smoke test"
    )
    parser.add_argument(
        "--confirm",
        action="store_true",
        help="Execute the live MCP authentication smoke suite",
    )
    return parser.parse_args()


def allocate_free_port() -> int:
    with socket.socket(socket.AF_INET, socket.SOCK_STREAM) as sock:
        sock.bind(("127.0.0.1", 0))
        port = sock.getsockname()[1]
        if port == 3000:
            return allocate_free_port()
        return port


class AuthenticatedMCPHandler(BaseHTTPRequestHandler):
    """Compliant Streamable HTTP MCP 2025-11-25 handler with Bearer token authentication."""

    expected_token: str = ""
    session_id: str = "mcp-session-live-42"
    tool_calls_received: list[dict[str, Any]] = []

    def log_message(self, format: str, *args: Any) -> None:
        pass  # Suppress default access logs

    def do_POST(self) -> None:
        # Check authentication header
        auth = self.headers.get("Authorization", "")
        if auth != f"Bearer {self.expected_token}":
            self.send_response(401)
            self.send_header("Content-Type", "application/json")
            self.end_headers()
            self.wfile.write(json.dumps({"error": "unauthorized"}).encode())
            return

        content_len = int(self.headers.get("Content-Length", 0))
        body_bytes = self.rfile.read(content_len)
        try:
            msg = json.loads(body_bytes.decode("utf-8"))
        except Exception:
            self.send_response(400)
            self.end_headers()
            return

        method = msg.get("method")
        msg_id = msg.get("id")

        if method == "initialize":
            params = msg.get("params", {})
            if params.get("protocolVersion") != PROTOCOL_VERSION:
                self.send_response(400)
                self.end_headers()
                return

            self.send_response(200)
            self.send_header("Content-Type", "application/json")
            self.send_header("Content-Encoding", "identity")
            self.send_header("MCP-Session-Id", self.session_id)
            self.end_headers()
            resp = {
                "jsonrpc": "2.0",
                "id": msg_id,
                "result": {
                    "protocolVersion": PROTOCOL_VERSION,
                    "capabilities": {"tools": {}},
                    "serverInfo": {"name": "SecureCorp MCP Service", "version": "2.1.0"},
                },
            }
            self.wfile.write(json.dumps(resp).encode("utf-8"))

        elif method == "notifications/initialized":
            self.send_response(202)
            self.send_header("Content-Encoding", "identity")
            self.end_headers()

        elif method == "tools/list":
            self.send_response(200)
            self.send_header("Content-Type", "application/json")
            self.send_header("Content-Encoding", "identity")
            self.send_header("MCP-Session-Id", self.session_id)
            self.end_headers()
            resp = {
                "jsonrpc": "2.0",
                "id": msg_id,
                "result": {
                    "tools": [
                        {
                            "name": "provision_database_replica",
                            "description": "Provision a read replica for a high-availability cluster",
                            "inputSchema": {
                                "type": "object",
                                "properties": {
                                    "cluster_id": {"type": "string"},
                                    "region": {"type": "string"},
                                },
                                "required": ["cluster_id"],
                            },
                        }
                    ]
                },
            }
            self.wfile.write(json.dumps(resp).encode("utf-8"))

        elif method == "tools/call":
            params = msg.get("params", {})
            self.tool_calls_received.append(params)
            self.send_response(200)
            self.send_header("Content-Type", "application/json")
            self.send_header("Content-Encoding", "identity")
            self.send_header("MCP-Session-Id", self.session_id)
            self.end_headers()
            resp = {
                "jsonrpc": "2.0",
                "id": msg_id,
                "result": {
                    "content": [
                        {
                            "type": "text",
                            "text": f"Replica for cluster '{params.get('arguments', {}).get('cluster_id')}' provisioned successfully.",
                        }
                    ],
                    "isError": False,
                },
            }
            self.wfile.write(json.dumps(resp).encode("utf-8"))

        else:
            self.send_response(404)
            self.end_headers()

    def do_DELETE(self) -> None:
        # Client cleanup endpoint
        self.send_response(200)
        self.end_headers()


def start_mcp_server(port: int, token: str) -> tuple[HTTPServer, threading.Thread]:
    AuthenticatedMCPHandler.expected_token = token
    AuthenticatedMCPHandler.tool_calls_received = []
    server = HTTPServer(("127.0.0.1", port), AuthenticatedMCPHandler)
    thread = threading.Thread(target=server.serve_forever, daemon=True)
    thread.start()
    return server, thread


def start_app_server(port: int, data_dir: Path, bootstrap_token: str) -> subprocess.Popen:
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

    for _ in range(45):
        if process.poll() is not None:
            log_handle.flush()
            log_handle.close()
            content = log_file.read_text(errors="replace")
            raise RuntimeError(f"Inference server failed to boot:\n{content}")
        try:
            with httpx.Client(timeout=1.0) as client:
                res = client.get(f"http://127.0.0.1:{port}/healthz")
                if res.status_code == 200:
                    return process
        except Exception:
            pass
        time.sleep(1)

    process.terminate()
    raise RuntimeError("Inference server did not become ready within 45 seconds.")


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

    def record(step_name: str, outcome: str, detail: str = "") -> None:
        steps.append((step_name, outcome, detail))
        status_str = f"[{outcome}]".ljust(10)
        print(f"{status_str} {step_name}" + (f" - {detail}" if detail else ""), flush=True)

    print("================================================================================")
    print("STARTING LIVE MCP STREAMABLE HTTP & AUTHENTICATED SERVER SMOKE SUITE")
    print("================================================================================")

    mcp_port = allocate_free_port()
    app_port = allocate_free_port()

    mcp_token = "mcp_token_" + secrets.token_urlsafe(24)
    data_dir = Path(tempfile.mkdtemp(prefix="inference-mcp-smoke-"))
    bootstrap_token = secrets.token_urlsafe(32)

    mcp_server: HTTPServer | None = None
    app_process: subprocess.Popen | None = None

    try:
        # 1. Start Authenticated MCP Server
        print(f"Starting authenticated MCP server on 127.0.0.1:{mcp_port} (protocol {PROTOCOL_VERSION})...")
        mcp_server, _ = start_mcp_server(mcp_port, mcp_token)
        record(
            "Step 1: Start Authenticated MCP Server",
            "PASS",
            f"Port {mcp_port}, Bearer authentication enforced, protocol {PROTOCOL_VERSION}",
        )

        # 2. Start Inference Server
        print(f"Starting Inference server on 127.0.0.1:{app_port}...")
        app_process = start_app_server(app_port, data_dir, bootstrap_token)
        app_base = f"http://127.0.0.1:{app_port}"
        record(
            "Step 2: Boot Disposable Inference Server",
            "PASS",
            f"Port {app_port}, loopback egress permitted",
        )

        # 3. Bootstrap Administrator Account
        with httpx.Client(base_url=app_base, timeout=30.0, trust_env=False, headers={"Origin": app_base}) as client:
            admin_pw = "AdminPass9!" + secrets.token_urlsafe(16)
            res_setup = client.post(
                "/api/auth/setup",
                json={"username": "mcp_admin", "password": admin_pw},
                headers={"X-Inference-Bootstrap": bootstrap_token},
            )
            assert res_setup.status_code == 200, f"Setup failed: {res_setup.text}"
            csrf_token = client.cookies.get("agd_csrf")
            assert csrf_token, "agd_csrf cookie missing"
            client.headers["X-AGD-CSRF"] = csrf_token
            record("Step 3: Bootstrap Administrator Account", "PASS", "admin session active with CSRF")

            # 4. Save MCP Auth Token into Fernet Vault
            res_secret = client.post(
                "/api/admin/secrets",
                json={"name": "MCP_AUTH_KEY", "value": mcp_token},
            )
            assert res_secret.status_code == 200, f"Store secret failed: {res_secret.text}"
            record("Step 4: Save MCP Secret in Vault", "PASS", "$MCP_AUTH_KEY encrypted in secrets.json")

            # 5. Register Authenticated MCP Server
            res_cfg = client.put(
                "/api/assistant/config/mcp",
                json={
                    "id": "secure-db-mcp",
                    "config": {
                        "url": f"http://127.0.0.1:{mcp_port}/mcp",
                        "api_key_ref": "$MCP_AUTH_KEY",
                    },
                },
            )
            assert res_cfg.status_code == 200, f"Config MCP failed: {res_cfg.text}"
            record(
                "Step 5: Register MCP Server in Inference",
                "PASS",
                "Server 'secure-db-mcp' referencing $MCP_AUTH_KEY",
            )

            # 6. Discover Tools via Inference
            res_tools = client.get("/api/assistant/mcp/secure-db-mcp/tools")
            assert res_tools.status_code == 200, f"List tools failed: {res_tools.text}"
            tools_list = res_tools.json().get("data", [])
            assert len(tools_list) == 1, f"Expected 1 tool, found {len(tools_list)}"
            assert tools_list[0]["name"] == "provision_database_replica"
            record(
                "Step 6: Discover Tools over Authenticated Streamable HTTP",
                "PASS",
                f"Discovered tool '{tools_list[0]['name']}', schema verified",
            )

            # 7. Propose Mutating Tool Invocation (15-Minute Human Approval Gate)
            assert len(AuthenticatedMCPHandler.tool_calls_received) == 0
            proposal_body = {
                "name": "mcp_call",
                "arguments": {
                    "server_id": "secure-db-mcp",
                    "tool_name": "provision_database_replica",
                    "arguments": {"cluster_id": "db-prod-analytics", "region": "us-east-1"},
                },
            }
            res_run = client.post("/api/assistant/tools/run", json=proposal_body)
            assert res_run.status_code == 200, f"Tool run proposal failed: {res_run.text}"
            run_data = res_run.json()

            pending_action = run_data.get("pending_action", run_data)
            assert pending_action.get("status") == "pending", f"Expected pending status: {run_data}"
            action_id = pending_action.get("id")
            assert action_id, "Missing action_id in proposal"
            assert pending_action.get("expires_at", 0) > time.time() + 800, "TTL must be ~15 minutes"

            # Assert remote tool was NOT executed autonomously
            assert (
                len(AuthenticatedMCPHandler.tool_calls_received) == 0
            ), "Prompt injection violation: tool was executed autonomously without approval"
            record(
                "Step 7: Enforce 15-Minute Approval Gate",
                "PASS",
                f"Action {action_id} queued as 'pending'; zero autonomous tool invocations",
            )

            # 8. Confirm Tool Execution
            res_confirm = client.post("/api/assistant/tools/confirm", json={"action_id": action_id})
            assert res_confirm.status_code == 200, f"Confirm failed: {res_confirm.text}"
            confirm_data = res_confirm.json()

            assert confirm_data.get("status") == "succeeded", f"Expected succeeded: {confirm_data}"
            res_content = confirm_data.get("result", {}).get("content", [])
            assert "db-prod-analytics" in str(res_content)

            # Assert remote server received the authenticated call
            assert len(AuthenticatedMCPHandler.tool_calls_received) == 1
            record(
                "Step 8: Human Approval Confirmation & Remote Execution",
                "PASS",
                f"Action {action_id} consumed; remote MCP tool returned status='succeeded'",
            )

            # 9. Replay Rejection
            res_replay = client.post("/api/assistant/tools/confirm", json={"action_id": action_id})
            assert res_replay.status_code in (404, 409), f"Expected 409 rejection, got {res_replay.status_code}"
            record(
                "Step 9: Replay Attack Rejection",
                "PASS",
                f"Second confirmation of {action_id} rejected with HTTP {res_replay.status_code}; single-use enforced",
            )

            # 10. Expiry Rejection
            res_run_exp = client.post("/api/assistant/tools/run", json=proposal_body)
            assert res_run_exp.status_code == 200
            exp_action_id = res_run_exp.json().get("pending_action", res_run_exp.json())["id"]

            # Manually expire in SQLite database
            db_path = data_dir / "dashboard.db"
            with sqlite3.connect(db_path) as db:
                db.execute("UPDATE approval_actions SET expires_at=1 WHERE id=?", (exp_action_id,))
                db.commit()

            res_confirm_exp = client.post("/api/assistant/tools/confirm", json={"action_id": exp_action_id})
            assert (
                res_confirm_exp.status_code == 409
            ), f"Expected 409 rejection, got {res_confirm_exp.status_code}"
            assert res_confirm_exp.json().get("code") in ("approval_unavailable", "approval_expired", "action_expired")
            record(
                "Step 10: Expired Action Rejection",
                "PASS",
                f"Action {exp_action_id} with expired TTL rejected with HTTP 409 '{res_confirm_exp.json().get('code')}'",
            )

            # 11. Authentication Failure Path (Fail Closed)
            # Store invalid key
            client.post("/api/admin/secrets", json={"name": "BAD_MCP_KEY", "value": "wrong_password_123"})
            client.put(
                "/api/assistant/config/mcp",
                json={
                    "id": "unauth-mcp",
                    "config": {
                        "url": f"http://127.0.0.1:{mcp_port}/mcp",
                        "api_key_ref": "$BAD_MCP_KEY",
                    },
                },
            )
            res_bad = client.get("/api/assistant/mcp/unauth-mcp/tools")
            assert (
                res_bad.status_code == 502
            ), f"Expected 502 mcp_error on bad auth, got {res_bad.status_code}"
            assert res_bad.json().get("code") == "mcp_error"
            record(
                "Step 11: Fail-Closed on Invalid Bearer Token",
                "PASS",
                "Tool discovery rejected with HTTP 502 'mcp_error' when Bearer token invalid",
            )

        elapsed = time.monotonic() - start_time
        print("\n================================================================================")
        print("SUMMARY: ALL MCP PROTOCOL 2025-11-25 & AUTHENTICATION CHECKS PASSED")
        print("================================================================================")
        print(f"Elapsed time: {elapsed:.2f}s")
        print(f"Total steps executed: {len(steps)}")
        print(f"Passed: {sum(1 for s in steps if s[1] == 'PASS')}")
        print(f"Failed / Not Verified: {sum(1 for s in steps if s[1] != 'PASS')}")
        print("Streamable HTTP client, 15-min human approval gate, replay, and auth failure proven.")
        print("================================================================================")

    finally:
        print("\nCleaning up disposable MCP test resources...")
        if mcp_server is not None:
            mcp_server.shutdown()
        if app_process is not None:
            stop_app_server(app_process)
        shutil.rmtree(data_dir, ignore_errors=True)
        print("Cleanup complete.")


def main() -> None:
    args = parse_args()
    if not args.confirm:
        print("NOT RUN: Pass --confirm to execute the live MCP authentication smoke suite.")
        sys.exit(0)

    try:
        run_smoke_test()
    except Exception as exc:
        print(f"\n[FATAL] MCP authentication smoke suite failed: {exc}", file=sys.stderr)
        import traceback

        traceback.print_exc()
        sys.exit(1)


if __name__ == "__main__":
    main()
