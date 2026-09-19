"""Load and performance benchmark for Inference.

Exercises dashboard aggregation, large error lists, session authentication,
and OTLP span ingestion under concurrency against tens of thousands of seeded rows
in a temporary data directory on a dynamically allocated loopback port.

Requires --confirm to run; without --confirm it prints NOT RUN and exits.
"""
from __future__ import annotations

import argparse
import asyncio
import gzip
import os
import secrets
import socket
import sqlite3
import subprocess
import sys
import tempfile
import time
from datetime import datetime, timezone
from pathlib import Path
from typing import Any

import httpx
from opentelemetry.proto.collector.trace.v1.trace_service_pb2 import (
    ExportTraceServiceRequest,
    ExportTraceServiceResponse,
)

ROOT = Path(__file__).resolve().parents[1]


def parse_args() -> argparse.Namespace:
    parser = argparse.ArgumentParser(description="Inference load and performance benchmark")
    parser.add_argument(
        "--confirm",
        action="store_true",
        help="Confirm execution of the load benchmark suite",
    )
    parser.add_argument(
        "--errors",
        type=int,
        default=25000,
        help="Number of error records to seed (default: 25000)",
    )
    parser.add_argument(
        "--spans",
        type=int,
        default=25000,
        help="Number of OTLP spans to seed (default: 25000)",
    )
    parser.add_argument(
        "--concurrency",
        type=int,
        default=10,
        help="Concurrent workers for request generation (default: 10)",
    )
    parser.add_argument(
        "--requests",
        type=int,
        default=100,
        help="Number of requests per benchmark stage (default: 100)",
    )
    return parser.parse_args()


def allocate_free_port() -> int:
    with socket.socket(socket.AF_INET, socket.SOCK_STREAM) as sock:
        sock.bind(("127.0.0.1", 0))
        return sock.getsockname()[1]


def start_server(port: int, folder: str, log_file: Path) -> subprocess.Popen:
    env = {
        **os.environ,
        "INFERENCE_DATA_DIR": folder,
        "INFERENCE_SECURE_COOKIES": "false",
        "INFERENCE_BOOTSTRAP_TOKEN": "",
        "INFERENCE_ALLOWED_ORIGINS": "",
    }
    env.pop("INFERENCE_SESSION_HOURS", None)
    with log_file.open("w") as log:
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
            stdout=log,
            stderr=subprocess.STDOUT,
            creationflags=getattr(subprocess, "CREATE_NO_WINDOW", 0),
        )
    return process


def stop_server(process: subprocess.Popen | None) -> None:
    if process is not None and process.poll() is None:
        if os.name == "nt":
            subprocess.run(
                ["taskkill", "/PID", str(process.pid), "/T", "/F"],
                stdout=subprocess.DEVNULL,
                stderr=subprocess.DEVNULL,
                check=False,
                creationflags=getattr(subprocess, "CREATE_NO_WINDOW", 0),
            )
        else:
            process.terminate()
        try:
            process.wait(timeout=10)
        except subprocess.TimeoutExpired:
            process.kill()
            process.wait(timeout=5)


def wait_healthy(client: httpx.Client, process: subprocess.Popen, log_file: Path, timeout: float = 15.0) -> None:
    deadline = time.monotonic() + timeout
    while time.monotonic() < deadline:
        if process.poll() is not None:
            raise RuntimeError(f"Server exited prematurely: {log_file.read_text(errors='replace')}")
        try:
            resp = client.get("/healthz")
            if resp.status_code == 200:
                return
        except httpx.TransportError:
            pass
        time.sleep(0.1)
    raise RuntimeError("Local backend did not become healthy within timeout")


def seed_database(db_path: Path, num_errors: int, num_spans: int) -> None:
    """Fast bulk insertion of tens of thousands of errors and spans."""
    print(f"[*] Seeding database directly: {num_errors:,} errors and {num_spans:,} spans...")
    start_time = time.monotonic()
    
    conn = sqlite3.connect(db_path)
    cur = conn.cursor()
    
    now_iso = datetime.now(timezone.utc).isoformat()
    
    # 1. Seed errors
    batch_size = 5000
    for offset in range(0, num_errors, batch_size):
        count = min(batch_size, num_errors - offset)
        rows = [
            (
                "inst-load",
                f"wf-{((offset + i) % 100):03d}",
                f"Production Workflow {((offset + i) % 100):03d}",
                f"exec-{offset + i}",
                f"Node-{((offset + i) % 25)}",
                "NodeApiError" if (offset + i) % 2 == 0 else "ExecutionError",
                f"Simulated load test error payload #{offset + i}: downstream timeout or HTTP 500",
                1,
                now_iso,
                "open",
                f"err-key-{offset + i}",
            )
            for i in range(count)
        ]
        cur.executemany(
            """INSERT INTO errors(
                instance_id, workflow_id, workflow_name, execution_id, node_name,
                error_type, error_message, count, occurred_at, status, error_key
            ) VALUES (?,?,?,?,?,?,?,?,?,?,?)""",
            rows,
        )
    
    # 2. Seed spans
    for offset in range(0, num_spans, batch_size):
        count = min(batch_size, num_spans - offset)
        rows = [
            (
                f"trace-{((offset + i) // 5):08x}",
                f"span-{offset + i:016x}",
                "",
                "inst-load",
                f"wf-{((offset + i) % 100):03d}",
                f"Production Workflow {((offset + i) % 100):03d}",
                f"exec-{(offset + i) // 5}",
                f"HTTP Request {((offset + i) % 15)}",
                1,
                (1700000000 + offset + i) * 1_000_000_000,
                (1700000000 + offset + i + 1) * 1_000_000_000,
                "error" if (offset + i) % 10 == 0 else "ok",
                '{"service.name":"n8n","deployment.environment":"load"}',
                now_iso,
                "gpt-4o",
                150,
                350,
                0.005,
                "error" if (offset + i) % 10 == 0 else "unknown",
                "NodeApiError" if (offset + i) % 10 == 0 else None,
                "HTTP request returned 502 Bad Gateway" if (offset + i) % 10 == 0 else None,
                1,
                1,
                f"node-{((offset + i) % 15)}",
                0,
                now_iso,
                "0.005000",
                '{"cost_usd":"0.005000","provenance":"price_book"}',
                f"HTTP Request {((offset + i) % 15)}",
            )
            for i in range(count)
        ]
        cur.executemany(
            """INSERT INTO otel_spans(
                trace_id, span_id, parent_id, instance_id, workflow_id, workflow_name,
                execution_id, name, kind, start_ns, end_ns, status, attributes_json,
                received_at, model, tokens_in, tokens_out, cost_usd, health_status,
                error_type, error_summary, output_items, input_items, node_id,
                silent, checked_at, cost_exact, pricing_json, node_name
            ) VALUES (?,?,?,?,?,?,?,?,?,?,?,?,?,?,?,?,?,?,?,?,?,?,?,?,?,?,?,?,?)""",
            rows,
        )
    
    conn.commit()
    conn.close()
    elapsed = time.monotonic() - start_time
    print(f"[*] Seeded {num_errors:,} errors and {num_spans:,} spans in {elapsed:.2f}s")


def calculate_percentiles(latencies_ms: list[float]) -> dict[str, float]:
    if not latencies_ms:
        return {"min": 0, "p50": 0, "p95": 0, "p99": 0, "max": 0, "mean": 0}
    sorted_lat = sorted(latencies_ms)
    n = len(sorted_lat)
    return {
        "min": sorted_lat[0],
        "p50": sorted_lat[int(n * 0.50)],
        "p95": sorted_lat[min(int(n * 0.95), n - 1)],
        "p99": sorted_lat[min(int(n * 0.99), n - 1)],
        "max": sorted_lat[-1],
        "mean": sum(sorted_lat) / n,
    }


def format_stats(name: str, total_time: float, latencies_ms: list[float], status_counts: dict[int, int]) -> str:
    p = calculate_percentiles(latencies_ms)
    rps = len(latencies_ms) / total_time if total_time > 0 else 0
    statuses = ", ".join(f"{code}:{cnt}" for code, cnt in sorted(status_counts.items()))
    return (
        f"  {name:<28} | {len(latencies_ms):>5} reqs | {rps:>7.1f} req/s | "
        f"p50: {p['p50']:>6.2f}ms | p95: {p['p95']:>6.2f}ms | p99: {p['p99']:>6.2f}ms | "
        f"max: {p['max']:>6.2f}ms | [{statuses}]"
    )


async def run_concurrent_workload(
    client: httpx.AsyncClient,
    method: str,
    path: str,
    total_requests: int,
    concurrency: int,
    headers: dict[str, str] | None = None,
    content: bytes | None = None,
    json_data: Any | None = None,
) -> tuple[float, list[float], dict[int, int]]:
    semaphore = asyncio.Semaphore(concurrency)
    latencies: list[float] = []
    status_counts: dict[int, int] = {}
    lock = asyncio.Lock()

    async def single_request() -> None:
        async with semaphore:
            start = time.perf_counter()
            try:
                resp = await client.request(
                    method=method,
                    url=path,
                    headers=headers,
                    content=content,
                    json=json_data,
                )
                status = resp.status_code
            except Exception:
                status = 0
            dur_ms = (time.perf_counter() - start) * 1000
            async with lock:
                latencies.append(dur_ms)
                status_counts[status] = status_counts.get(status, 0) + 1

    t0 = time.perf_counter()
    tasks = [asyncio.create_task(single_request()) for _ in range(total_requests)]
    await asyncio.gather(*tasks)
    total_time = time.perf_counter() - t0
    return total_time, latencies, status_counts


def create_otlp_batch_payload(span_count: int = 5) -> bytes:
    req = ExportTraceServiceRequest()
    rs = req.resource_spans.add()
    ss = rs.scope_spans.add()
    for _ in range(span_count):
        span = ss.spans.add()
        span.trace_id = secrets.token_bytes(16)
        span.span_id = secrets.token_bytes(8)
        span.name = "LoadTestSpan"
        span.start_time_unix_nano = time.time_ns()
        span.end_time_unix_nano = time.time_ns() + 50_000_000
    return gzip.compress(req.SerializeToString())


async def async_main(args: argparse.Namespace, port: int, cookies: dict[str, str], csrf_token: str, otlp_token: str) -> dict[str, Any]:
    base_url = f"http://127.0.0.1:{port}"
    benchmark_results = {}

    auth_headers = {"Cookie": f"inference_session={cookies['inference_session']}; agd_csrf={csrf_token}"}
    otlp_headers = {
        "Authorization": f"Bearer {otlp_token}",
        "Content-Type": "application/x-protobuf",
        "Content-Encoding": "gzip",
    }

    limits = httpx.Limits(max_connections=args.concurrency * 2, max_keepalive_connections=args.concurrency)
    timeout = httpx.Timeout(15.0, connect=5.0)

    async with httpx.AsyncClient(base_url=base_url, limits=limits, timeout=timeout, trust_env=False) as client:
        print("\n======================= INFERENCE LOAD BENCHMARK =======================")
        print(f"Target: {base_url} | Concurrency: {args.concurrency} workers | Requests per test: {args.requests}")
        print("------------------------------------------------------------------------")

        # 1. Observability Summary & Aggregation over 25,000 Spans
        t, lats, statuses = await run_concurrent_workload(
            client, "GET", "/api/otel/summary", args.requests, args.concurrency, headers=auth_headers
        )
        out = format_stats("Observability Summary", t, lats, statuses)
        print(out)
        benchmark_results["otel_summary"] = {"time": t, "percentiles": calculate_percentiles(lats), "statuses": statuses}

        # 2. Observability Traces Aggregation (GROUP BY trace_id, MIN/MAX/COUNT over spans)
        t, lats, statuses = await run_concurrent_workload(
            client, "GET", "/api/otel/traces?limit=50", args.requests, args.concurrency, headers=auth_headers
        )
        out = format_stats("Traces List (Page 50)", t, lats, statuses)
        print(out)
        benchmark_results["otel_traces"] = {"time": t, "percentiles": calculate_percentiles(lats), "statuses": statuses}

        # 3. Large Error List (50 errors from 25,000 seeded)
        t, lats, statuses = await run_concurrent_workload(
            client, "GET", "/api/errors?limit=50", args.requests, args.concurrency, headers=auth_headers
        )
        out = format_stats("Error List (Page 50)", t, lats, statuses)
        print(out)
        benchmark_results["errors_page_50"] = {"time": t, "percentiles": calculate_percentiles(lats), "statuses": statuses}

        # 4. Large Error List (100 errors from 25,000 seeded)
        t, lats, statuses = await run_concurrent_workload(
            client, "GET", "/api/errors?limit=100&status=open", args.requests, args.concurrency, headers=auth_headers
        )
        out = format_stats("Error List (Page 100)", t, lats, statuses)
        print(out)
        benchmark_results["errors_page_100"] = {"time": t, "percentiles": calculate_percentiles(lats), "statuses": statuses}

        # 5. Session Authentication Verification (auth_sessions lookup + role check)
        t, lats, statuses = await run_concurrent_workload(
            client, "GET", "/api/auth/status", args.requests, args.concurrency, headers=auth_headers
        )
        out = format_stats("Session Auth (/auth/status)", t, lats, statuses)
        print(out)
        benchmark_results["session_auth_status"] = {"time": t, "percentiles": calculate_percentiles(lats), "statuses": statuses}

        # 6. Authenticated Role Recheck (/api/admin/users)
        t, lats, statuses = await run_concurrent_workload(
            client, "GET", "/api/admin/users", args.requests, args.concurrency, headers=auth_headers
        )
        out = format_stats("Admin Role Check (/users)", t, lats, statuses)
        print(out)
        benchmark_results["admin_users_role_check"] = {"time": t, "percentiles": calculate_percentiles(lats), "statuses": statuses}

        # 7. Dashboard Proxy Aggregation (hitting limiter, demonstrating rate limiting protection)
        # Note: /api/n8n_proxy/dashboard has per-user limit of 4 per 60s
        t, lats, statuses = await run_concurrent_workload(
            client, "GET", "/api/n8n_proxy/dashboard", 20, 2, headers=auth_headers
        )
        out = format_stats("Dashboard Proxy (Limiter)", t, lats, statuses)
        print(out)
        benchmark_results["dashboard_proxy"] = {"time": t, "percentiles": calculate_percentiles(lats), "statuses": statuses}

        # 8. OTLP Span Ingestion Under Concurrency (Protobuf Gzip Batches)
        # Ingestion is limited to 120/min per token; test concurrent batch delivery
        payload = create_otlp_batch_payload(span_count=5)
        t, lats, statuses = await run_concurrent_workload(
            client, "POST", "/api/otel/v1/traces", 50, args.concurrency, headers=otlp_headers, content=payload
        )
        out = format_stats("OTLP Ingestion (5-span batch)", t, lats, statuses)
        print(out)
        benchmark_results["otlp_ingestion"] = {"time": t, "percentiles": calculate_percentiles(lats), "statuses": statuses}

        print("========================================================================\n")

    return benchmark_results


def main() -> int:
    args = parse_args()
    if not args.confirm:
        print("NOT RUN (run with --confirm to execute load benchmark)")
        return 0

    port = allocate_free_port()
    with tempfile.TemporaryDirectory(prefix="inference-load-") as temp_dir:
        db_path = Path(temp_dir) / "dashboard.db"
        log_file = Path(temp_dir) / "server.log"
        password = secrets.token_urlsafe(24)
        server: subprocess.Popen | None = None

        print(f"[*] Starting isolated Inference server on 127.0.0.1:{port}...")
        server = start_server(port, temp_dir, log_file)

        try:
            with httpx.Client(base_url=f"http://127.0.0.1:{port}", timeout=5.0, trust_env=False) as sync_client:
                wait_healthy(sync_client, server, log_file)
                print("[*] Server healthy. Setting up admin account...")

                # 1. Setup admin user
                setup_resp = sync_client.post(
                    "/api/auth/setup",
                    json={"username": "loadtester", "password": password},
                )
                if setup_resp.status_code != 200:
                    raise RuntimeError(f"Setup failed: {setup_resp.status_code} {setup_resp.text}")

                cookies = dict(sync_client.cookies)
                csrf_token = cookies.get("agd_csrf", "")
                csrf_headers = {"X-AGD-CSRF": csrf_token}

                # 2. Register an instance for telemetry attribution
                inst_resp = sync_client.post(
                    "/api/n8n_proxy/instances",
                    headers=csrf_headers,
                    json={
                        "name": "Load Test Instance",
                        "url": "https://load-fixture.invalid",
                        "api_key": secrets.token_urlsafe(24),
                    },
                )
                if inst_resp.status_code != 200:
                    raise RuntimeError(f"Failed to create instance: {inst_resp.status_code} {inst_resp.text}")
                instance_id = inst_resp.json()["id"]

                # 3. Create OTLP machine token
                tok_resp = sync_client.post(
                    "/api/otel/tokens",
                    headers=csrf_headers,
                    json={"instance_id": instance_id, "name": "Load Token"},
                )
                if tok_resp.status_code != 200:
                    raise RuntimeError(f"Failed to create telemetry token: {tok_resp.status_code} {tok_resp.text}")
                otlp_token = tok_resp.json()["token"]

            # 4. Stop server momentarily to seed database cleanly without contention
            print("[*] Pausing server for high-speed batch seeding...")
            stop_server(server)
            server = None

            # Seed tens of thousands of errors and spans
            seed_database(db_path, num_errors=args.errors, num_spans=args.spans)

            # 5. Restart server
            print("[*] Restarting server with populated database...")
            server = start_server(port, temp_dir, log_file)
            with httpx.Client(base_url=f"http://127.0.0.1:{port}", timeout=5.0, trust_env=False) as sync_client:
                wait_healthy(sync_client, server, log_file)

            # 6. Run async load benchmarks
            asyncio.run(async_main(args, port, cookies, csrf_token, otlp_token))
            print("[OK] Load benchmark completed successfully.")

        finally:
            if server is not None:
                stop_server(server)

    return 0


if __name__ == "__main__":
    sys.exit(main())
