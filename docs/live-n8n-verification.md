# Live n8n and runner verification

Run from the repository with Docker Desktop's Linux engine available:

```powershell
docker build -t inference:local .
.venv/Scripts/python.exe scripts/smoke_live_n8n.py
```

The script creates a uniquely named Inference container with temporary application data and access to the Linux Docker socket. It provisions n8n 2.19.0 and matching runners through the real Docker manager API, checks readiness and idempotent replay, creates a temporary owner/API key, and executes a webhook through JavaScript and native Python Code nodes. It checks execution visibility through the fleet proxy and waits up to 60 seconds for instance-scoped native OTLP spans. Credentials are generated in memory and never printed. `N8N_TEST_VERSION` can explicitly select another exact version; this does not imply that version has been verified.

The test uses Docker Desktop host forwarding for OTLP and loopback-published ports for the host test client. After provisioning, it joins its temporary Inference container to the new bundle network and saves `http://n8n:5678` as the instance URL. This direct network route is required in the tested environment: proxy requests through the host-published address returned 502. The temporary network attachment is disconnected before bundle cleanup. Native Linux host networking and socket group permissions need a separate environment adaptation. First-run image downloads can take several minutes. No existing application data or host containers are adopted. Cleanup uses the new installation's ownership-checked bundle deletion API, including only its disposable volume. An incomplete cleanup retains the test application container and prints its name for reconciliation. No global Docker prune is performed.

Sources: [runner setup](https://github.com/n8n-io/n8n-docs/blob/main/docs/deploy/host-n8n/configure-n8n/set-up-task-runners.md), [2.19.0 OTLP configuration](https://github.com/n8n-io/n8n/blob/n8n%402.19.0/packages/cli/src/modules/otel/otel.config.ts).

Actual results and limitations are recorded in IMPLEMENTATION.md; the script alone is not proof of successful integration.

Verified 2026-09-14 on Docker Desktop Linux engine with n8n/runners 2.19.0: managed readiness, idempotent provisioning, native JS/Python output, execution proxy visibility, instance-scoped received OTLP spans and ownership-checked cleanup passed. The local HTTP harness explicitly forwards its generated n8n setup cookie; production Secure-cookie settings remain enabled. This verifies received telemetry, not every span attribute, silent-failure classification or load behavior.
