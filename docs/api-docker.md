# Docker bundle API

The Docker manager controls only bundles recorded by this Inference installation. Every route under `/api/docker_mgr` requires an administrator; mutations also require session CSRF and Origin validation.

## Runtime and ownership

Set `INFERENCE_DOCKER_SOCKET` to an absolute Unix socket path, default `/var/run/docker.sock`. The service uses a lifespan-owned `aiodocker` client. It does not discover `DOCKER_HOST`, use a TCP Docker endpoint, or fall back to Windows named pipes. Run Inference on Linux/WSL or in a Linux container with the configured socket accessible. An unavailable socket returns a redacted `503 docker_unavailable`; other modules still start.

Ownership requires all four labels: `com.inference.managed=true`, persistent installation owner ID, persisted bundle ID and expected resource role. Container/network mutations use full IDs after fresh inspection. Generic Compose labels never authorize an action. The API exposes safe metadata, not container environment variables or raw logs. It never prunes host resources or replaces an existing container by name.

## Provision an n8n and runner pair

`POST /bundles`:

```json
{
  "spec": {
    "name": "development",
    "version": "2.19.0",
    "port": 5678,
    "bind_host": "127.0.0.1",
    "exporter_base_url": "https://inference.example/api/otel"
  },
  "instance_url": "http://127.0.0.1:5678",
  "idempotency_key": "development-deploy-001",
  "readiness_timeout": 90
}
```

Use addresses reachable from the respective processes: `instance_url` is what Inference uses to reach n8n; `exporter_base_url` is what the n8n container uses to reach Inference. Loopback inside a container refers to that container. Permit private fleet destinations explicitly with `INFERENCE_EGRESS_ALLOW_CIDRS`; the example loopback fleet URL needs `127.0.0.1/32`. Default host publication is loopback; bind a different literal IP explicitly when required.

Version is an exact numeric tag, at least `1.111.0`; floating tags and arbitrary images are rejected. The bundle uses `docker.n8n.io/n8nio/n8n:<version>` and `n8nio/runners:<version>`. Images are pulled only on an explicit provision request. Templates do not accept arbitrary environment variables, host bind mounts, privileged mode or a Docker socket mount.

The pair gets a dedicated labeled bridge network, a persistent named volume mounted at `/home/node/.n8n`, shared generated runner authentication, external runner mode and a private broker on port 5679. Only n8n port 5678 is published; the broker remains private. Both JavaScript and Python runners launch. Templates drop Linux capabilities, prohibit privilege escalation, cap memory/CPU/PIDs and rotate container logs.

Kahn ordering validates dependencies before creation. n8n starts first and must pass `/healthz/readiness`; the runner launcher then starts and must pass `/healthz` on port 5680. Readiness checks have an overall timeout of 30–300 seconds per container, including inspection time. Container health does not prove successful Code node execution or telemetry delivery.

A local instance and hashed per-instance telemetry token are created transactionally before Docker effects; generated credentials and the deployment plan are encrypted in `documents`. **Complete n8n owner setup and add its public API key through the Instances API before fleet operations.** The initial instance accurately reports no configured key.

Native n8n OTLP configuration includes the exporter base, `/v1/traces` path and `N8N_OTEL_EXPORTER_OTLP_HEADERS=Authorization=Bearer ...`, supported by the verified [n8n 2.19.0 configuration](https://github.com/n8n-io/n8n/blob/n8n%402.19.0/packages/cli/src/modules/otel/otel.config.ts). Older accepted runner versions receive an explicit instrumentation warning. Setup injection is not proof of trace delivery. Runner command/health configuration was checked against the official [runner image](https://github.com/n8n-io/n8n/blob/n8n%402.19.0/docker/images/runners/Dockerfile) and [launcher setup](https://github.com/n8n-io/task-runner-launcher/blob/main/docs/setup.md).

## Observe and control

| Route | Behavior |
|---|---|
| `GET /status` | Check the configured Docker service; 503 when unavailable |
| `GET /bundles` | Latest 100 persisted bundle summaries, even when Docker is down |
| `GET /bundles/{id}` | Status, stage, resource IDs, warnings and observations |
| `GET /containers` | Safe inventory filtered to this installation's persisted bundles |
| `POST /bundles/{id}/start` | Start in dependency order and wait for health |
| `POST /bundles/{id}/stop` | Stop runners before n8n |
| `POST /bundles/{id}/reconcile` | Inspect expected owned resources and persist observations; no remote writes |
| `DELETE /bundles/{id}` | Stop/remove owned containers and network; preserve the data volume |
| `DELETE /bundles/{id}?purge_data=true` | Also explicitly remove the owned data volume |

Deletion revokes the bundle's telemetry token. The local instance record is retained so the administrator can inspect or remove it separately. A removed bundle with a retained volume can receive a later explicit purge request. No generic volume cleanup occurs.

## Failure and recovery

Deployment journals each stage before the external effect. The idempotency key belongs to the actor and exact request: matching repeats return the saved outcome; different settings return `409 idempotency_conflict`. A repeated uncertain request never creates another container.

Statuses include `deploying`, `ready`, `partial`, `uncertain`, `starting`, `stopped`, `stopping`, `deleting`, `removed`. Known failures retain completed resources. Transport failures/cancellation after possible writes and interrupted operations at application restart become uncertain; no rollback or retry is implied. A missing ownership record with existing bundles fails startup rather than claiming a new installation identity.

For an uncertain outcome, call `/reconcile` first. It can discover an owned resource by its deterministic expected name if a create response was lost, then records its full ID. Ownership mismatches block recovery rather than adopting the resource. Incomplete deployments can be inspected and explicitly removed; provisioning does not silently resume them. Inspection alone does not revoke telemetry tokens; an explicit deletion does.

Network ownership and volume ownership are rechecked before each container creation; containers attach by canonical network ID. Docker's name-based volume API has no conditional create/mount/delete primitive. A host administrator can race these operations or alter local storage; application ownership labels cannot defend against that administrator.

## Verification boundary

Tests use SDK fixtures and a loopback simulated Docker Engine through the installed `aiodocker` client. They cover ownership/replacement, canonical IDs, pulls, readiness, idempotency, partial outcomes, cancellation/restart, role changes and volume preservation/purge. No real Docker images, containers, networks or volumes were changed during this unit. Docker Desktop's local Linux engine was unreachable. Live n8n/runner execution, native OTLP delivery and Docker delivery configuration remain separate checks.

Live follow-up (2026-09-14): the real Docker Desktop Linux integration now passes readiness/replay, JS and native Python execution, proxy execution visibility, native OTLP arrival and cleanup for 2.19.0. See [live verification](live-n8n-verification.md) for the tested direct-network topology and remaining coverage limits. Earlier fixture-only statements above describe the original implementation unit.
