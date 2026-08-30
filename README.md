# home-harness

An agent harness for the home server that does not depend on which LLM you plug
into it. Talk to it by voice from a phone or a laptop; it controls Home
Assistant and manages Google Calendar.

Runs on the same k3d cluster as [arr-stack](https://github.com/Franion03/arr-stack),
managed by the same ArgoCD, with secrets sealed by the same controller.

```
  phone / laptop (PWA)
          │  HTTPS — hold to talk
          ▼
  ┌───────────────────────────────────────────┐
  │  harness  (namespace: assistant)          │
  │                                           │
  │   FastAPI  ─ /v1/chat  /v1/voice          │
  │      │                                    │
  │   agent loop ── tools ──┬── Home Assistant│──▶ 192.168.1.117:8123
  │      │                  └── Google Calendar│──▶ Calendar API v3
  │      │                                    │
  │   router ── route → primary, fallback     │
  │      │      (from models.yaml ConfigMap)  │
  │      ▼                                    │
  │   adapters: anthropic │ openai │ google   │
  └──────────┬────────────────────────────────┘
             ▼
   Cloudflare AI Gateway  ── caching, cost analytics, rate limits
             ▼
   Anthropic · OpenAI · Gemini · OpenRouter · Groq · Workers AI
```

## Why it is LLM-agnostic

No file in `harness/app/` outside `provider_*.py` names a vendor or a model.
The agent loop, the tools and the API all speak one canonical vocabulary —
`Message`, `ToolSpec`, `ToolUse`, `ToolResult`, `Completion` — defined in
`provider_base.py`. Each adapter's only job is translating that to and from one
vendor's wire format.

Which model answers lives in **`deploy/base/models.yaml`**, mounted as a
ConfigMap. Changing your mind about the LLM looks like this:

```yaml
routes:
  chat:
    primary: anthropic/claude-opus-5     # ← change this line
    fallback: openai/gpt-4o
```

```bash
kubectl -n assistant rollout restart deploy/harness
# or, with no restart at all:
curl -XPOST -H "X-API-Key: $KEY" http://assistant.192.168.1.114.nip.io/v1/admin/reload
```

No code change, no rebuild, no redeploy. The same applies to speech: `stt:` and
`tts:` are model references with fallbacks, exactly like the chat routes.

Adding a vendor that nobody has written an adapter for yet is one file plus one
line in `build_registry()`. If it speaks `/v1/chat/completions`, it is just a
line — `provider_openai.py` already covers OpenAI, OpenRouter, Groq, Mistral,
DeepSeek and Cloudflare Workers AI.

**Fallbacks are for availability, not cost.** When the primary rate-limits or
5xxs, the route's fallback answers, so the house keeps working through a vendor
outage. Point it at a *different vendor* than the primary. A 4xx is treated as
non-retryable — a malformed request would fail identically on the fallback.

## Layout

```
harness/app/               flat modules — same source runs from an image or a ConfigMap
  provider_base.py         canonical types every layer above speaks
  provider_anthropic.py    native Messages API (tool use, thinking, effort)
  provider_openai.py       /v1/chat/completions — 6 vendors, one adapter
  provider_google.py       Gemini generateContent
  gateway.py               Cloudflare AI Gateway transport
  llm.py                   registry + fallback router
  agent.py                 the tool loop
  tool_registry.py         JSON Schema tool descriptions + dispatch
  tool_homeassistant.py    entities, states, services, HA's own intent engine
  tool_calendar.py         Calendar API v3 over plain HTTPS
  google_auth.py           refresh token → access token
  speech.py                STT and TTS, same route-and-fallback pattern
  memory.py                per-session history in SQLite on the PVC
  main.py                  FastAPI surface
  static/                  the voice PWA
harness/tests/             28 tests, standard library only
deploy/base/               kustomize base + models.yaml
deploy/overlays/ghcr/      production: image from GHCR   ← ArgoCD points here
deploy/overlays/live/      registry-free: source from ConfigMaps
scripts/                   OAuth consent, secrets, ConfigMap rendering
```

## API

| Method | Path | Purpose |
|---|---|---|
| `GET` | `/` | the voice PWA |
| `GET` | `/health` | liveness + the active configuration (unauthenticated) |
| `POST` | `/v1/chat` | `{message, session_id?, route?, speak?}` → text (+ audio) |
| `POST` | `/v1/voice` | multipart audio → transcript, answer, spoken reply |
| `POST` | `/v1/transcribe` | multipart audio → text |
| `POST` | `/v1/speak` | `{text}` → audio bytes |
| `GET` | `/v1/sessions` | recent conversations |
| `DELETE` | `/v1/sessions/{id}` | forget a conversation |
| `POST` | `/v1/admin/reload` | re-read `models.yaml` without restarting |

Everything except `/health` and the PWA requires `X-API-Key` (or
`Authorization: Bearer`) matching `HARNESS_API_KEY`.

## Deploying

### Registry-free (what is running now)

Runs a stock `python:3.12-slim` with the source mounted from ConfigMaps, so it
works before any image exists.

```bash
scripts/render-source-configmap.sh    # re-run after any change under harness/
kubectl apply -k deploy/overlays/live
```

### GitOps (the target state)

```bash
git remote add origin git@github.com:Franion03/home-harness.git
git push -u origin master           # GH Actions builds and pushes to GHCR
kubectl apply -f argocd/application.yaml
```

ArgoCD then owns the `assistant` namespace with `prune` and `selfHeal`, the
same as `arr-stack-root`.

### Secrets

Local bootstrap:

```bash
export ANTHROPIC_API_KEY=... OPENAI_API_KEY=... HA_TOKEN=...
scripts/create-secrets.sh
kubectl -n assistant rollout restart deploy/harness
```

GitOps: put the same values in GitHub Secrets and run the **Seal Secrets**
workflow — identical pipeline to arr-stack. `scripts/seal-secrets.sh` does the
same thing from your shell.

`/health` reports which integrations came up, so a missing credential is
visible rather than silent.

## Voice from a phone

The PWA uses `MediaRecorder`, which browsers only expose in a **secure
context**. Over `http://assistant.192.168.1.114.nip.io` the microphone will be
blocked and the page says so; typing still works.

Give it HTTPS by adding a hostname to the cloudflared tunnel already running in
the `media` namespace — Cloudflare Zero Trust → Networks → Tunnels → your
tunnel → Public Hostname:

```
  assistant.<your-domain>  →  http://harness.assistant.svc.cluster.local:80
```

Put a Cloudflare Access policy in front of it, then open the URL on the phone
and *Add to Home Screen*. Hold the big button to talk; on a laptop, hold the
space bar.

## Tests

```bash
python harness/tests/test_harness.py
```

Covers the fallback router, the tool loop (including parallel calls, tool
failures and the iteration cap), history repair, and each adapter's wire
format. No network, no API keys, standard library only.
