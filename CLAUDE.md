# home-harness — notes for Claude

LLM-agnostic agent harness on the home k3d cluster. Voice + Home Assistant +
Google Calendar.

## The invariant that matters

**No vendor or model name may appear outside `harness/app/provider_*.py`,
`llm.py`'s `build_registry()`, and `deploy/base/models.yaml`.**

If a change would put `claude`, `gpt`, `gemini`, an `anthropic.` import or a
vendor URL into `agent.py`, `main.py`, `tool_*.py` or `speech.py`, it is the
wrong change. Add or extend an adapter instead. The canonical vocabulary is in
`provider_base.py`; everything above it is vendor-blind by construction.

## Architecture in one pass

- `provider_base.py` — `Message`, `Text`, `ToolUse`, `ToolResult`, `ToolSpec`,
  `Completion`, `ProviderError(retryable=...)`. The `Provider` protocol is a
  single `complete()` method.
- `gateway.py` — all HTTP egress. Builds Cloudflare AI Gateway URLs
  (`/v1/{account}/{gateway}/{provider}/{path}`) or, with
  `AI_GATEWAY_MODE=direct`, each vendor's own base URL. Classifies 4xx as
  non-retryable so the router does not waste a fallback on a bad request.
- `llm.py` — `split_ref()` splits `provider/model` on the **first** slash only
  (OpenRouter and Workers AI model ids contain their own). `Router.complete()`
  walks `[primary, fallback]`.
- `agent.py` — the loop. Tool results for one assistant turn go back in a
  **single** user message; splitting them teaches models to stop batching.
- `memory.py` — SQLite on the PVC. `_repair()` trims a window that starts with
  an orphan `tool_result` or ends with an unanswered `tool_use`; every provider
  rejects those.

## Provider gotchas that are already handled — do not "fix" them

- **Anthropic**: Opus 5 / Sonnet 5 / Fable 5 / Opus 4.6-4.8 reject
  `temperature` with a 400. `NO_SAMPLING_PREFIXES` in `provider_anthropic.py`
  drops it. They also reject `thinking.budget_tokens`; use
  `thinking: adaptive` and `output_config.effort`.
- **Google**: the assistant role is `model`, not `assistant`. Function calls
  carry no id, so ids are synthesised as `name:index` and decoded back in
  `_encode_message`. `_clean_schema()` strips `additionalProperties`, `$schema`
  and `default`, which Gemini 400s on.
- **OpenAI**: one canonical `Message` holding tool results expands into several
  `role: "tool"` messages. Tool arguments arrive as a **JSON string** and can be
  malformed — always `json.loads` in a try block.

## Conventions

- Flat module layout in `harness/app/` on purpose: ConfigMap keys cannot
  contain `/`, and the `live` overlay mounts the source directly. Do not
  reorganise into subpackages without also dropping that overlay.
- Dependencies stay at fastapi + uvicorn + httpx + pydantic + PyYAML +
  python-multipart. Google Calendar is raw REST specifically to avoid
  `google-api-python-client`.
- Tests are standard-library `unittest`, no network, no keys:
  `python harness/tests/test_harness.py`.
- After editing anything under `harness/`, re-run
  `scripts/render-source-configmap.sh` before `kubectl apply -k deploy/overlays/live`.

## Cluster facts

- Server `192.168.1.114`, k3d cluster `arr-cluster`, context `k3d-arr-cluster`.
- Namespace `assistant`. Ingress `assistant.192.168.1.114.nip.io`, class nginx.
- Home Assistant is a **separate box at `192.168.1.117:8123`**, not in the
  cluster.
- sealed-secrets controller is in `kube-system` (shared with arr-stack).
- The PVC is `local-path` RWO, so the Deployment strategy must stay `Recreate`.
