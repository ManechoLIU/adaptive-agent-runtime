# External Agent authentication and execution

Adaptive Delivery owns this workflow end to end. Task Navigator and Codex Continuity are never required for login, credential discovery, preflight, or external model execution. Kimi Code CLI and Grok Build CLI remain the official model runners and must be installed separately.

## Supported routes

| Runner | `auth_mode` | Model | Credential source |
| --- | --- | --- | --- |
| `kimi-code` | `oauth` | `kimi-code/k3` | Kimi Code managed login/session |
| `kimi-code` | `api` | `kimi-k3` | Kimi Open Platform key via `KIMI_MODEL_API_KEY`, `MOONSHOT_API_KEY`, or OS credential store |
| `grok-build` | `oauth` | `grok-4.6` | Grok Build OAuth session |
| `grok-build` | `api` | `grok-4.6` | `XAI_API_KEY` or OS credential store |

The same model can have different allowance and billing behavior under OAuth and API. Require the route contract to specify `auth_mode`; never infer it from an installed executable, cached login, detected key, or model name.

## Login and credential setup

Resolve `scripts/run_external_agent.mjs` relative to this Skill directory.

OAuth login launches the official CLI flow and requires explicit current authorization:

```text
node <skill-directory>/scripts/run_external_agent.mjs --login --authorized-login --engine kimi-code --auth-mode oauth --region mainland-cn --cwd <repository-root>
node <skill-directory>/scripts/run_external_agent.mjs --login --authorized-login --engine kimi-code --auth-mode oauth --region global --cwd <repository-root>
node <skill-directory>/scripts/run_external_agent.mjs --login --authorized-login --engine grok-build --auth-mode oauth --cwd <repository-root>
node <skill-directory>/scripts/run_external_agent.mjs --login --authorized-login --device-auth --engine grok-build --auth-mode oauth --cwd <repository-root>
```

For API mode, never request that the user paste a key into chat. CI and managed environments may provide `KIMI_MODEL_API_KEY`/`MOONSHOT_API_KEY` or `XAI_API_KEY` to the execution process. On macOS, a user can store keys interactively without putting the secret in shell history:

```text
security add-generic-password -U -s adaptive-delivery-kimi-k3 -a "$USER" -w
security add-generic-password -U -s adaptive-delivery-xai-grok -a "$USER" -w
```

Kimi K3 API uses the Kimi Open Platform endpoint `https://api.moonshot.cn/v1` for mainland China and the API model ID `kimi-k3`; this route is pay-as-you-go and does not require a Kimi membership. The global Open Platform uses `https://api.moonshot.ai/v1`. Create the key on the matching Open Platform because platform credentials and endpoints are not interchangeable. `KIMI_K3_BASE_URL` can explicitly override the region-derived endpoint.

## No-call preflight

Run only the route selected by the contract:

```text
node <skill-directory>/scripts/run_external_agent.mjs --check --engine kimi-code --auth-mode oauth --model kimi-code/k3 --reasoning-effort <selected-effort> --cwd <repository-root>
node <skill-directory>/scripts/run_external_agent.mjs --check --engine kimi-code --auth-mode api --model kimi-k3 --reasoning-effort <selected-effort> --cwd <repository-root>
node <skill-directory>/scripts/run_external_agent.mjs --check --engine grok-build --auth-mode oauth --model grok-4.6 --reasoning-effort <selected-effort> --cwd <repository-root>
node <skill-directory>/scripts/run_external_agent.mjs --check --engine grok-build --auth-mode api --model grok-4.6 --reasoning-effort <selected-effort> --cwd <repository-root>
```

`available` proves only executable and version discovery. `credentialConfigured` proves only that the expected local credential source exists. Neither proves subscription entitlement, balance, model access, successful inference, or billing behavior.

## Visible execution status

External runners execute inside the controller task and do not create a native Codex subagent card. Never compose a card by hand. After preflight succeeds and immediately before a real authorized request, call the deterministic renderer and copy its stdout verbatim into a user-visible commentary message:

```text
node <skill-directory>/scripts/run_external_agent.mjs --render-status-card --engine <kimi-code|grok-build> --auth-mode <oauth|api> --model <exact-model> --reasoning-effort <selected-effort> --work-package <stable-id> --category <frontend|backend|general> --status running --detail <short-single-line-detail>
```

For the terminal card, call the same renderer with exactly one of `returned`, `accepted`, `blocked`, or `unknown`. The renderer owns provider colors, human-readable model labels, state icons, Chinese state labels, the three-line frame, and single-line field validation. Do not edit its output, translate the state, replace its icons, or use free-form states such as `ACTIVE` or `REVIEWING`. If rendering fails, stop before the external request and report the renderer error without inventing a colored card. Do not publish a card for `--check` or while authorization is still missing.

Emit at most one generated start card and one generated terminal card per call, with no periodic refresh. If parent validation finishes in the same short event, skip `已返回` and publish only `已验收`; otherwise `已返回` carries the candidate boundary until validation completes. Publish `已验收` only after the parent has inspected the actual diff and completed the route contract's verification. A preflight failure uses `blocked`; an interrupted or ambiguous request that may have consumed allowance or produced partial edits uses `unknown` and stops without automatic retry. Never include credentials or sensitive raw output in these cards.

## Paid execution gate

Immediately before a real model request, confirm that the current user request authorizes the selected model, `auth_mode`, scope, and expected paid or allowance-consuming call. Prior installation, login, key configuration, or general delegation consent is insufficient.

Build the smallest self-contained prompt from the route contract and pipe it on standard input:

```text
node <skill-directory>/scripts/run_external_agent.mjs --execute --authorized-external-call --engine kimi-code --auth-mode oauth --model kimi-code/k3 --reasoning-effort <selected-effort> --cwd <repository-root>
node <skill-directory>/scripts/run_external_agent.mjs --execute --authorized-external-call --engine kimi-code --auth-mode api --model kimi-k3 --reasoning-effort <selected-effort> --cwd <repository-root>
node <skill-directory>/scripts/run_external_agent.mjs --execute --authorized-external-call --engine grok-build --auth-mode oauth --model grok-4.6 --reasoning-effort <selected-effort> --cwd <repository-root>
node <skill-directory>/scripts/run_external_agent.mjs --execute --authorized-external-call --engine grok-build --auth-mode api --model grok-4.6 --reasoning-effort <selected-effort> --cwd <repository-root>
```

`<selected-effort>` must be one of `low`, `medium`, `high`, `xhigh`, or `max`. The adapter rejects a missing or unsupported value. It passes the selection as `KIMI_MODEL_THINKING_EFFORT` to Kimi and `--reasoning-effort` to Grok, so neither runner silently falls back to its configured default.

The Grok API route uses a temporary isolated `GROK_HOME` so a cached OAuth session cannot take precedence. The Grok OAuth route removes `XAI_API_KEY` from the child environment. The Kimi OAuth route removes temporary API-model variables; the Kimi API route injects the selected model only in the child process.

Do not retry a possibly charged failure automatically. Treat external edits as candidates: inspect the actual diff, confirm file ownership, run the contract checks, and complete required real-entry acceptance in the parent.
