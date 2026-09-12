"""Skeleton: live AI repair verification against a managed n8n 2.19 bundle and a real tool-calling provider.

Manual only. It is not part of any suite and refuses to start without --confirm-live. The browser
scenario (scripts/frontend_repair_checks.py) covers the same flow against a simulator; this check
exists to confirm the n8n public API and a real model behave the same way.

Planned run (each step reports PASS, or NOT VERIFIED with the reason; nothing is retried):
 1. Provision a disposable Inference container and managed n8n bundle as scripts/smoke_live_n8n.py does
    (owner setup, scoped API key, loopback-only ports, ownership-checked cleanup).
 2. Create a workflow like the simulator's wf-orders: Webhook -> HTTP Request -> Code (`return item;`).
    The HTTP Request calls an unknown n8n webhook path (HTTP 404) with a per-run fake secret
    (`sk-live-<random>`) in an Authorization header parameter. Activate it and call the webhook once.
 3. Find the failed execution through Inference and pin a repair target (instance, workflow, execution).
 4. Read GET /api/assistant/repair/context: the fake secret is absent, `[redacted` is present.
 5. Tool-calling check on the provider (one request). Then wait the pacing interval.
 6. Repair chat: one model request per human action. A read-tool turn must stop at awaiting_continue;
    Continue sends one more request only after the pacing interval. Record the proposal, if any.
 7. Apply: linked deactivate (one POST), Apply (one PUT). Confirm with the workflow's version history that
    exactly one new version exists and the readback matches; a replayed confirm returns 409.
 8. Undo: restore proposal from the repair snapshot, Restore version (one PUT); content equals the
    pre-repair version. Activation stays a separate, explicit confirmation.
 9. Search chat history, approvals and context responses for the fake secret; it must be absent.
10. Remove the bundle and container through the ownership-checked API.

Provider pacing: hy3 (api.b.ai) answered a back-to-back request with HTTP 429 and the same request
60 seconds later with 200, without rate-limit headers. Keep at least 60 seconds between provider
requests. A 429 stops the run and is reported; it is never retried automatically.
"""
import argparse
import secrets
import sys
import time

MIN_PACE_SECONDS = 60
N8N_VERSION = '2.19.0'


class Pacer:
    """Enforces a minimum interval between provider requests. Never retries."""

    def __init__(self, seconds):
        if seconds < MIN_PACE_SECONDS:
            raise ValueError(f'Pacing must be at least {MIN_PACE_SECONDS} seconds.')
        self.seconds = seconds
        self.last = None

    def before_request(self):
        if self.last is not None:
            remaining = self.seconds - (time.monotonic() - self.last)
            if remaining > 0:
                print(f'Waiting {remaining:.0f} s before the next provider request.', flush=True)
                time.sleep(remaining)
        self.last = time.monotonic()


class ProviderRateLimited(RuntimeError):
    """Raised on a provider 429. The run stops; no automatic retry."""


def checked(response):
    """Raise with method, path, status and error code only; response bodies may contain secrets."""
    if not response.is_success:
        code = 'non_json'
        if response.headers.get('content-type', '').startswith('application/json'):
            code = response.json().get('code', 'unknown')
        if code in ('provider_rate_limited', 'assistant_rate_limit'):
            raise ProviderRateLimited(f'{response.request.method} {response.request.url.path}: HTTP {response.status_code} ({code})')
        raise RuntimeError(f'{response.request.method} {response.request.url.path}: HTTP {response.status_code} ({code})')
    return response.json()


def fake_secret():
    return 'sk-live-' + secrets.token_hex(12)


STEPS = (
    'provision_bundle', 'create_failing_workflow', 'pin_target', 'check_context_masking', 'check_tool_calling',
    'repair_conversation', 'apply_with_deactivate', 'undo_with_restore', 'search_for_secret', 'cleanup',
)


def not_implemented(step):
    raise NotImplementedError(f'Live repair step "{step}" is not wired yet; see the module docstring.')


def parse_args(argv):
    parser = argparse.ArgumentParser(description=__doc__.splitlines()[0])
    parser.add_argument('--confirm-live', action='store_true',
                        help='Required. Creates disposable containers and sends real provider requests.')
    parser.add_argument('--plan', action='store_true', help='Print the planned steps and exit.')
    parser.add_argument('--provider-id', default='hy3', help='Saved connection to verify (default: hy3).')
    parser.add_argument('--provider-key-ref', help='Vault or environment reference for the provider key; never a raw key.')
    parser.add_argument('--pace-seconds', type=int, default=MIN_PACE_SECONDS,
                        help=f'Seconds between provider requests (minimum {MIN_PACE_SECONDS}).')
    parser.add_argument('--n8n-version', default=N8N_VERSION)
    return parser.parse_args(argv)


def main(argv=None):
    args = parse_args(sys.argv[1:] if argv is None else argv)
    if args.plan:
        for number, step in enumerate(STEPS, 1):
            print(f'{number:2}. {step}', flush=True)
        return 0
    if not args.confirm_live:
        print('NOT RUN: live repair verification needs --confirm-live (disposable containers, real provider requests).', flush=True)
        return 0
    Pacer(args.pace_seconds)
    for step in STEPS:
        not_implemented(step)
    return 1


if __name__ == '__main__':
    sys.exit(main())
