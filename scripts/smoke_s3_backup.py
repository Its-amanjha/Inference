"""Live MinIO S3 backup and disaster-recovery smoke test for Inference.

Validates an end-to-end round-trip against a real local MinIO S3 container:
1. Spawns MinIO in Docker on an ephemeral port with generated credentials.
2. Starts Inference in a fresh temporary data directory with loopback egress allowed.
3. Seeds real data: instance, notes, encrypted secret, and S3 backup schedule.
4. Executes backup run, uploads to MinIO via AWS SigV4, and verifies size/sha256.
5. Downloads the object from MinIO and performs offline restore into an empty directory.
6. Verifies all database records, decrypted secrets, and notes in the restored target.
7. Validates five distinct failure paths:
   - S3 endpoint unreachable mid-upload (unknown remote outcome, local archive kept)
   - Invalid credentials (HTTP 403, rejected, local archive kept)
   - Nonexistent bucket (HTTP 404, rejected, local archive kept)
   - Corrupted archive byte (authentication failure, destination untouched)
   - Wrong passphrase on restore (authentication failure, destination untouched)
8. Tears down all containers and temporary directories.

Requires --confirm to run; without --confirm it prints NOT RUN and exits 0.
"""
from __future__ import annotations

import argparse
from datetime import datetime, timezone
import hashlib
import hmac
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
import time
from typing import Any

import httpx

MINIO_IMAGE = "quay.io/minio/minio:RELEASE.2024-01-18T22-51-28Z"
ROOT = Path(__file__).resolve().parents[1]
if str(ROOT) not in sys.path:
    sys.path.insert(0, str(ROOT))


def parse_args() -> argparse.Namespace:
    parser = argparse.ArgumentParser(
        description="Live MinIO S3 backup and disaster-recovery smoke test"
    )
    parser.add_argument(
        "--confirm",
        action="store_true",
        help="Execute the live S3 backup smoke suite against local MinIO",
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


def start_minio_container(
    name: str, port: int, user: str, password: str, bucket: str
) -> None:
    run = docker_cmd(
        "run",
        "-d",
        "--name",
        name,
        "-e",
        f"MINIO_ROOT_USER={user}",
        "-e",
        f"MINIO_ROOT_PASSWORD={password}",
        "-p",
        f"127.0.0.1:{port}:9000",
        MINIO_IMAGE,
        "server",
        "/data",
    )
    if run.returncode != 0:
        raise RuntimeError(f"Failed to start MinIO container: {run.stderr.strip()}")

    # Wait for MinIO HTTP readiness
    ready = False
    for _ in range(30):
        try:
            with httpx.Client(timeout=1.0) as client:
                res = client.get(f"http://127.0.0.1:{port}/minio/health/live")
                if res.status_code == 200:
                    ready = True
                    break
        except Exception:
            pass
        time.sleep(1)

    if not ready:
        raise RuntimeError("MinIO container did not become ready within 30 seconds.")

    # Create bucket in /data
    mk = docker_cmd("exec", name, "mkdir", "-p", f"/data/{bucket}")
    if mk.returncode != 0:
        raise RuntimeError(f"Failed to create MinIO bucket {bucket}: {mk.stderr.strip()}")


def stop_minio_container(name: str) -> None:
    docker_cmd("stop", name)


def remove_minio_container(name: str) -> None:
    docker_cmd("rm", "-f", name)


def s3_download_object(
    endpoint: str,
    bucket: str,
    prefix: str,
    key: str,
    access_key: str,
    secret_key: str,
    region: str = "us-east-1",
) -> bytes:
    object_path = "/".join(filter(None, (bucket, prefix, key)))
    url = f"{endpoint.rstrip('/')}/{object_path}"
    parsed = httpx.URL(url)
    now = datetime.now(timezone.utc)
    timestamp, day = now.strftime("%Y%m%dT%H%M%SZ"), now.strftime("%Y%m%d")
    empty_sha = "e3b0c44298fc1c149afbf4c8996fb92427ae41e4649b934ca495991b7852b855"
    headers = {
        "host": parsed.netloc.decode("ascii"),
        "x-amz-content-sha256": empty_sha,
        "x-amz-date": timestamp,
    }
    names = ";".join(sorted(headers))
    canonical = "".join(f"{name}:{headers[name]}\n" for name in sorted(headers))
    canonical_request = "\n".join(
        ("GET", parsed.raw_path.decode("ascii"), "", canonical, names, empty_sha)
    )
    scope = f"{day}/{region}/s3/aws4_request"
    to_sign = "\n".join(
        ("AWS4-HMAC-SHA256", timestamp, scope, hashlib.sha256(canonical_request.encode()).hexdigest())
    )

    def _sign(k: bytes, v: str) -> bytes:
        return hmac.new(k, v.encode("utf-8"), hashlib.sha256).digest()

    signing = _sign(_sign(_sign(_sign(("AWS4" + secret_key).encode(), day), region), "s3"), "aws4_request")
    signature = _sign(signing, to_sign).hex()
    headers["authorization"] = f"AWS4-HMAC-SHA256 Credential={access_key}/{scope}, SignedHeaders={names}, Signature={signature}"
    with httpx.Client(timeout=30.0) as client:
        resp = client.get(url, headers=headers)
        if resp.status_code != 200:
            raise RuntimeError(f"S3 GET failed with status {resp.status_code}: {resp.text}")
        return resp.content


def start_app_server(
    port: int, data_dir: Path, bootstrap_token: str
) -> subprocess.Popen:
    env = {
        **os.environ,
        "INFERENCE_DATA_DIR": str(data_dir),
        "INFERENCE_SECURE_COOKIES": "false",
        "INFERENCE_BOOTSTRAP_TOKEN": bootstrap_token,
        "INFERENCE_ALLOWED_ORIGINS": f"http://127.0.0.1:{port}",
        "INFERENCE_EGRESS_ALLOW_CIDRS": "127.0.0.1/32",
    }
    env.pop("INFERENCE_SESSION_HOURS", None)
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
        creationflags=getattr(subprocess, "CREATE_NO_WINDOW", 0),
    )
    # Store handle on process so caller can close later
    process._log_handle = log_handle  # type: ignore

    # Wait for server readiness
    ready = False
    for _ in range(30):
        try:
            with httpx.Client(timeout=1.0) as client:
                res = client.get(f"http://127.0.0.1:{port}/healthz")
                if res.status_code == 200:
                    ready = True
                    break
        except Exception:
            pass
        time.sleep(0.5)

    if not ready:
        stop_app_server(process)
        raise RuntimeError(f"App server on port {port} failed to start.")

    return process


def stop_app_server(process: subprocess.Popen | None) -> None:
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
                process.wait(timeout=5)
            except subprocess.TimeoutExpired:
                process.kill()
        if hasattr(process, "_log_handle"):
            try:
                process._log_handle.close()  # type: ignore
            except Exception:
                pass


def run_smoke_test() -> None:
    print("================================================================================")
    print("INFERENCE LIVE S3 BACKUP & DISASTER-RECOVERY VERIFICATION REPORT")
    print("================================================================================")
    print(f"Timestamp: {time.strftime('%Y-%m-%d %H:%M:%SZ', time.gmtime())}")
    print(f"MinIO Image: {MINIO_IMAGE}")

    minio_container = f"inference-s3-test-{secrets.token_hex(4)}"
    minio_port = allocate_free_port()
    minio_user = "minio_admin_" + secrets.token_hex(4)
    minio_password = "MinioPassword_" + secrets.token_hex(8)
    minio_bucket = "test-inference-backup-" + secrets.token_hex(4)

    app_port = allocate_free_port()
    bootstrap_token = secrets.token_urlsafe(32)
    admin_password = "AdminTestPassword123!"

    src_tmp = tempfile.mkdtemp(prefix="inference-s3-src-")
    dest_tmp = tempfile.mkdtemp(prefix="inference-s3-dest-")
    src_data_dir = Path(src_tmp)
    dest_data_dir = Path(dest_tmp)

    app_process: subprocess.Popen | None = None
    restored_process: subprocess.Popen | None = None

    steps: list[tuple[str, str, str]] = []

    def record(step: str, status: str, detail: str) -> None:
        steps.append((step, status, detail))
        print(f"[{status}] {step}: {detail}")

    try:
        # Step 1: Start MinIO
        start_minio_container(
            minio_container, minio_port, minio_user, minio_password, minio_bucket
        )
        record(
            "MinIO Container Startup",
            "PASS",
            f"Container {minio_container} listening on 127.0.0.1:{minio_port}, bucket '{minio_bucket}'",
        )

        # Step 2: Start App Server
        app_process = start_app_server(app_port, src_data_dir, bootstrap_token)
        record(
            "Inference Server Startup",
            "PASS",
            f"PID {app_process.pid} listening on 127.0.0.1:{app_port}, data_dir={src_data_dir}",
        )

        base_url = f"http://127.0.0.1:{app_port}"
        with httpx.Client(base_url=base_url, timeout=30.0) as client:
            # Step 3: Admin Bootstrap
            setup_res = client.post(
                "/api/auth/setup",
                json={"username": "backup_admin", "password": admin_password},
                headers={"X-Inference-Bootstrap": bootstrap_token},
            )
            if setup_res.status_code != 200:
                raise RuntimeError(f"Admin setup failed: {setup_res.text}")
            csrf = client.cookies.get("agd_csrf", "")
            headers = {"X-AGD-CSRF": csrf}
            record("Admin Account Bootstrap", "PASS", "Created admin user 'backup_admin' with session CSRF")

            # Step 4: Seed Real Content
            # 4a: Instance
            inst_res = client.post(
                "/api/n8n_proxy/instances",
                headers=headers,
                json={
                    "name": "Production Cluster Alpha",
                    "url": "http://127.0.0.1:5678",
                    "api_key": "live-instance-api-secret-key-9999",
                },
            )
            if inst_res.status_code != 200:
                raise RuntimeError(f"Instance creation failed: {inst_res.text}")
            instance_id = inst_res.json()["id"]
            record("Seed Instance", "PASS", f"Created instance ID {instance_id} with encrypted API key")

            # 4b: Note
            note_path = "runbooks/disaster-recovery.md"
            note_content = "# Disaster Recovery Runbook\n\nVerified via live MinIO S3 backup and restore."
            note_res = client.put(
                "/api/notes/note",
                headers=headers,
                json={"path": note_path, "content": note_content},
            )
            if note_res.status_code != 200:
                raise RuntimeError(f"Note creation failed: {note_res.text}")
            record("Seed Markdown Note", "PASS", f"Created note '{note_path}' ({len(note_content)} bytes)")

            # 4c: Encrypted Vault Secrets
            export_passphrase = "SecretBackupPassphrase2026_Secure!"
            sec1 = client.post(
                "/api/admin/secrets",
                headers=headers,
                json={"name": "BACKUP_PASSWORD", "value": export_passphrase},
            )
            if sec1.status_code != 200:
                raise RuntimeError(f"Failed to store BACKUP_PASSWORD: {sec1.text}")

            sec2 = client.post(
                "/api/admin/secrets",
                headers=headers,
                json={
                    "name": "S3_CREDS",
                    "value": {
                        "access_key_id": minio_user,
                        "secret_access_key": minio_password,
                    },
                },
            )
            if sec2.status_code != 200:
                raise RuntimeError(f"Failed to store S3_CREDS: {sec2.text}")

            sec3 = client.post(
                "/api/admin/secrets",
                headers=headers,
                json={"name": "APP_TOKEN", "value": "internal-secret-token-xyz-123"},
            )
            if sec3.status_code != 200:
                raise RuntimeError(f"Failed to store APP_TOKEN: {sec3.text}")
            record("Seed Encrypted Secrets", "PASS", "Saved $BACKUP_PASSWORD, $S3_CREDS, and $APP_TOKEN into Fernet vault")

            # Step 5: Configure S3 Schedule via API
            sched_body = {
                "name": "Hourly MinIO S3 Backup",
                "enabled": True,
                "interval_minutes": 60,
                "passphrase_ref": "$BACKUP_PASSWORD",
                "s3": {
                    "endpoint": f"http://127.0.0.1:{minio_port}",
                    "bucket": minio_bucket,
                    "region": "us-east-1",
                    "prefix": "fleet-backups",
                    "credentials_ref": "$S3_CREDS",
                },
            }
            sched_res = client.post("/api/backups/schedules", headers=headers, json=sched_body)
            if sched_res.status_code != 200:
                raise RuntimeError(f"Failed to configure S3 schedule: {sched_res.text}")
            schedule_id = sched_res.json()["id"]
            record("Configure S3 Backup Schedule", "PASS", f"Schedule ID {schedule_id} configured with endpoint http://127.0.0.1:{minio_port}")

            # Step 6: Trigger Manual Backup Run
            run_res = client.post(f"/api/backups/schedules/{schedule_id}/run", headers=headers)
            if run_res.status_code != 200:
                raise RuntimeError(f"Failed to execute backup run: {run_res.text}")
            run_data = run_res.json()
            run_id = run_data["id"]
            run_status = run_data["status"]
            remote_status = run_data.get("remote_status")
            size_bytes = run_data.get("size_bytes")
            sha256 = run_data.get("sha256")

            if run_status != "succeeded" or remote_status != "uploaded":
                raise RuntimeError(f"Backup run did not succeed: {run_data}")
            record(
                "Trigger Live Backup Run",
                "PASS",
                f"Run ID {run_id} status='succeeded', remote_status='uploaded', size={size_bytes}B, sha256={sha256[:16]}...",
            )

            # Step 7: Verify Object in MinIO Bucket and Download Over S3 Protocol
            minio_object_path = f"/data/{minio_bucket}/fleet-backups/{run_id}.ibackup"
            ls_res = docker_cmd("exec", minio_container, "ls", "-la", minio_object_path)
            if ls_res.returncode != 0:
                raise RuntimeError(f"Object not found in MinIO: {ls_res.stderr.strip()}")

            minio_bytes = s3_download_object(
                endpoint=f"http://127.0.0.1:{minio_port}",
                bucket=minio_bucket,
                prefix="fleet-backups",
                key=f"{run_id}.ibackup",
                access_key=minio_user,
                secret_key=minio_password,
            )

            minio_sha = hashlib.sha256(minio_bytes).hexdigest()
            if len(minio_bytes) != size_bytes or minio_sha != sha256:
                raise RuntimeError(
                    f"MinIO object verification mismatch: size {len(minio_bytes)} vs {size_bytes}, sha {minio_sha} vs {sha256}"
                )
            record(
                "Verify S3 Object in MinIO",
                "PASS",
                f"Object verified on MinIO disk ({minio_object_path}) and downloaded over S3 with matching size ({len(minio_bytes)}B) and SHA-256",
            )

            # Step 8: Offline Restore into Second Empty Data Directory
            from backend.modules.backups.restore import restore_archive
            from backend.config import Vault

            restore_archive(minio_bytes, export_passphrase, dest_data_dir)
            record(
                "Offline Restore",
                "PASS",
                f"Restored {len(minio_bytes)} bytes into empty destination {dest_data_dir}",
            )

            # Step 9: Verify Restored Data State
            # 9a: Check filesystem files
            assert (dest_data_dir / "dashboard.db").exists(), "dashboard.db missing in restored dir"
            assert (dest_data_dir / ".secret_key").exists(), ".secret_key missing in restored dir"
            assert (dest_data_dir / "secrets.json").exists(), "secrets.json missing in restored dir"
            restored_note = dest_data_dir / "notes" / note_path
            assert restored_note.exists(), "Restored note file missing"
            assert restored_note.read_text(encoding="utf-8") == note_content, "Restored note content mismatch"
            record("Verify Restored Filesystem", "PASS", "Verified dashboard.db, .secret_key, secrets.json, and note runbook")

            # 9b: Check SQLite database content
            with sqlite3.connect(dest_data_dir / "dashboard.db") as db:
                db.row_factory = sqlite3.Row
                users = db.execute("SELECT username, role, disabled FROM users").fetchall()
                assert len(users) == 1 and users[0]["username"] == "backup_admin", "Users row mismatch"

                instances = db.execute("SELECT id, name, url, api_key_enc FROM instances").fetchall()
                assert len(instances) == 1 and instances[0]["id"] == instance_id, "Instances row mismatch"

                schedules = db.execute("SELECT id, name FROM backup_schedules").fetchall()
                assert len(schedules) == 1 and schedules[0]["id"] == schedule_id, "Schedules row mismatch"

                runs = db.execute("SELECT id, status, remote_status, sha256 FROM backup_runs").fetchall()
                assert len(runs) == 1 and runs[0]["id"] == run_id, "Runs row mismatch"

            record(
                "Verify Restored Database Rows",
                "PASS",
                f"Confirmed 1 user ('backup_admin'), 1 instance ('{instances[0]['name']}'), 1 schedule, 1 run in SQLite",
            )

            # 9c: Check Vault decryption in restored environment
            restored_vault = Vault(dest_data_dir)
            assert restored_vault.resolve("$APP_TOKEN") == "internal-secret-token-xyz-123", "Restored APP_TOKEN mismatch"
            assert restored_vault.resolve("$BACKUP_PASSWORD") == export_passphrase, "Restored BACKUP_PASSWORD mismatch"
            decrypted_api_key = restored_vault.decrypt(instances[0]["api_key_enc"])
            assert decrypted_api_key == "live-instance-api-secret-key-9999", "Decrypted API key mismatch"
            record(
                "Verify Restored Vault Decryption",
                "PASS",
                f"Decrypted $APP_TOKEN and instance API key ('{decrypted_api_key[:12]}...') matching source values",
            )

            # 9d: Spin up application against restored data directory
            restored_port = allocate_free_port()
            restored_process = start_app_server(
                restored_port, dest_data_dir, secrets.token_urlsafe(32)
            )
            with httpx.Client(base_url=f"http://127.0.0.1:{restored_port}", timeout=10.0) as rest_client:
                h_res = rest_client.get("/healthz")
                assert h_res.status_code == 200, "Restored app healthz failed"
                # Authenticate using the restored admin credentials
                auth_res = rest_client.post(
                    "/api/auth/login",
                    json={"username": "backup_admin", "password": admin_password},
                )
                assert auth_res.status_code == 200, "Restored login failed"
            stop_app_server(restored_process)
            restored_process = None
            record(
                "Boot Restored Application",
                "PASS",
                f"Started restored Inference process on 127.0.0.1:{restored_port}, /healthz=200, login successful",
            )

            # =========================================================================
            # TASK B: FAILURE PATH VERIFICATION
            # =========================================================================
            print("\n--------------------------------------------------------------------------------")
            print("TASK B: FAILURE PATH VERIFICATION")
            print("--------------------------------------------------------------------------------")

            # Failure Path 1: S3 endpoint unreachable / connection failure
            # Configure schedule pointing to an unreachable port
            unreachable_port = allocate_free_port()
            fail1_sched = client.post(
                "/api/backups/schedules",
                headers=headers,
                json={
                    "name": "Unreachable Endpoint Schedule",
                    "enabled": True,
                    "interval_minutes": 60,
                    "passphrase_ref": "$BACKUP_PASSWORD",
                    "s3": {
                        "endpoint": f"http://127.0.0.1:{unreachable_port}",
                        "bucket": minio_bucket,
                        "region": "us-east-1",
                        "prefix": "fleet-backups",
                        "credentials_ref": "$S3_CREDS",
                    },
                },
            )
            assert fail1_sched.status_code == 200
            fail1_sched_id = fail1_sched.json()["id"]

            fail1_run = client.post(f"/api/backups/schedules/{fail1_sched_id}/run", headers=headers)
            assert fail1_run.status_code == 200
            f1_data = fail1_run.json()
            assert f1_data["status"] == "partial", f"Expected partial, got {f1_data['status']}"
            assert f1_data["code"] == "s3_upload_failed", f"Expected s3_upload_failed, got {f1_data['code']}"
            assert f1_data["remote_status"] == "unknown", f"Expected unknown, got {f1_data['remote_status']}"
            f1_local = src_data_dir / "backups" / f"{f1_data['id']}.ibackup"
            assert f1_local.exists(), "Local archive must be preserved on unreachable upload"
            record(
                "Failure Path 1: S3 Endpoint Unreachable",
                "PASS",
                f"status='{f1_data['status']}', code='{f1_data['code']}', remote_status='{f1_data['remote_status']}', local archive preserved",
            )

            # Failure Path 2: Invalid Credentials (HTTP 403)
            client.post(
                "/api/admin/secrets",
                headers=headers,
                json={
                    "name": "BAD_S3_CREDS",
                    "value": {
                        "access_key_id": minio_user,
                        "secret_access_key": "wrong-secret-access-key-xyz",
                    },
                },
            )
            fail2_sched = client.post(
                "/api/backups/schedules",
                headers=headers,
                json={
                    "name": "Invalid Credentials Schedule",
                    "enabled": True,
                    "interval_minutes": 60,
                    "passphrase_ref": "$BACKUP_PASSWORD",
                    "s3": {
                        "endpoint": f"http://127.0.0.1:{minio_port}",
                        "bucket": minio_bucket,
                        "region": "us-east-1",
                        "prefix": "fleet-backups",
                        "credentials_ref": "$BAD_S3_CREDS",
                    },
                },
            )
            assert fail2_sched.status_code == 200
            fail2_sched_id = fail2_sched.json()["id"]

            fail2_run = client.post(f"/api/backups/schedules/{fail2_sched_id}/run", headers=headers)
            assert fail2_run.status_code == 200
            f2_data = fail2_run.json()
            assert f2_data["status"] == "partial", f"Expected partial, got {f2_data['status']}"
            assert f2_data["code"] == "s3_upload_failed", f"Expected s3_upload_failed, got {f2_data['code']}"
            assert f2_data["remote_status"] == "rejected", f"Expected rejected, got {f2_data['remote_status']}"
            f2_local = src_data_dir / "backups" / f"{f2_data['id']}.ibackup"
            assert f2_local.exists(), "Local archive must be preserved on 403 rejected upload"
            record(
                "Failure Path 2: Wrong S3 Credentials (403)",
                "PASS",
                f"status='{f2_data['status']}', code='{f2_data['code']}', remote_status='{f2_data['remote_status']}', local archive preserved",
            )

            # Failure Path 3: Bucket Does Not Exist (HTTP 404)
            fail3_sched = client.post(
                "/api/backups/schedules",
                headers=headers,
                json={
                    "name": "Nonexistent Bucket Schedule",
                    "enabled": True,
                    "interval_minutes": 60,
                    "passphrase_ref": "$BACKUP_PASSWORD",
                    "s3": {
                        "endpoint": f"http://127.0.0.1:{minio_port}",
                        "bucket": "nonexistent-bucket-999",
                        "region": "us-east-1",
                        "prefix": "fleet-backups",
                        "credentials_ref": "$S3_CREDS",
                    },
                },
            )
            assert fail3_sched.status_code == 200
            fail3_sched_id = fail3_sched.json()["id"]

            fail3_run = client.post(f"/api/backups/schedules/{fail3_sched_id}/run", headers=headers)
            assert fail3_run.status_code == 200
            f3_data = fail3_run.json()
            assert f3_data["status"] == "partial", f"Expected partial, got {f3_data['status']}"
            assert f3_data["code"] == "s3_upload_failed", f"Expected s3_upload_failed, got {f3_data['code']}"
            assert f3_data["remote_status"] == "rejected", f"Expected rejected, got {f3_data['remote_status']}"
            f3_local = src_data_dir / "backups" / f"{f3_data['id']}.ibackup"
            assert f3_local.exists(), "Local archive must be preserved on 404 nonexistent bucket"
            record(
                "Failure Path 3: Nonexistent Bucket (404)",
                "PASS",
                f"status='{f3_data['status']}', code='{f3_data['code']}', remote_status='{f3_data['remote_status']}', local archive preserved",
            )

            # Failure Path 4: Archive Corrupted After Upload
            from backend.modules.backups.archive import ArchiveError

            corrupted_bytes = bytearray(minio_bytes)
            # Flip byte 45 (inside ciphertext/salt portion)
            corrupted_bytes[45] ^= 0xFF
            corrupt_dest = Path(tempfile.mkdtemp(prefix="inference-s3-corrupt-"))
            corrupt_failed = False
            try:
                restore_archive(bytes(corrupted_bytes), export_passphrase, corrupt_dest)
            except ArchiveError as exc:
                corrupt_failed = True
                assert "authentication failed" in str(exc).lower()
            finally:
                assert corrupt_failed, "Corrupted archive must fail authentication"
                assert not list(corrupt_dest.iterdir()), "Corrupted restore must not populate destination"
                shutil.rmtree(corrupt_dest, ignore_errors=True)
            record(
                "Failure Path 4: Corrupted Archive Bit Flip",
                "PASS",
                "ArchiveError: 'Backup authentication failed', destination directory remained empty",
            )

            # Failure Path 5: Wrong Passphrase on Restore
            wrong_pw_dest = Path(tempfile.mkdtemp(prefix="inference-s3-wrong-pw-"))
            wrong_pw_failed = False
            try:
                restore_archive(minio_bytes, "CompletelyWrongExportPassphrase123!", wrong_pw_dest)
            except ArchiveError as exc:
                wrong_pw_failed = True
                assert "authentication failed" in str(exc).lower()
            finally:
                assert wrong_pw_failed, "Wrong passphrase must fail authentication"
                assert not list(wrong_pw_dest.iterdir()), "Wrong passphrase restore must not populate destination"
                shutil.rmtree(wrong_pw_dest, ignore_errors=True)
            record(
                "Failure Path 5: Wrong Passphrase on Restore",
                "PASS",
                "ArchiveError: 'Backup authentication failed', destination directory remained empty",
            )

        print("\n================================================================================")
        print("SUMMARY: ALL S3 BACKUP & DISASTER-RECOVERY CHECKS PASSED")
        print("================================================================================")
        print(f"Total steps executed: {len(steps)}")
        print(f"Passed: {sum(1 for s in steps if s[1] == 'PASS')}")
        print(f"Failed / Not Verified: {sum(1 for s in steps if s[1] != 'PASS')}")
        print("All local archives retained on upload failures; zero false-success claims.")
        print("================================================================================")

    finally:
        # Tear down processes and containers
        if app_process is not None:
            stop_app_server(app_process)
        if restored_process is not None:
            stop_app_server(restored_process)
        remove_minio_container(minio_container)
        shutil.rmtree(src_tmp, ignore_errors=True)
        shutil.rmtree(dest_tmp, ignore_errors=True)


def main() -> None:
    args = parse_args()
    if not args.confirm:
        print("NOT RUN: Pass --confirm to execute the live MinIO S3 backup and recovery smoke suite.")
        sys.exit(0)

    try:
        run_smoke_test()
    except Exception as exc:
        print(f"\n[FATAL] S3 backup smoke suite failed: {exc}", file=sys.stderr)
        import traceback

        traceback.print_exc()
        sys.exit(1)


if __name__ == "__main__":
    main()
