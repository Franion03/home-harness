# home-harness

An agent harness that does not depend on which LLM you plug into it. Talk to it
by voice from a phone or a laptop; it controls Home Assistant and manages
Google Calendar.

**This repo builds the harness. It does not deploy it.** Deployment lives in
[arr-stack/apps/harness](https://github.com/Franion03/arr-stack/tree/master/apps/harness),
which ArgoCD syncs onto the home cluster.

| Concern | Where |
|---|---|
| Python source, Dockerfile, tests, CI image build | here |
| Manifests, ingress, storage, secrets, live `models.yaml` | `Franion03/arr-stack` → `apps/harness/` |

CI publishes `ghcr.io/franion03/home-harness:latest` on every push to `master`.

```
  phone / laptop (PWA)
          │  HTTPS — hold to talk
          ▼
  ┌───────────────────────────────────────────┐
  │  harness                                  │
  │                                           │
  │   FastAPI  ─ /v1/chat  /v1/voice          │
  │      │                                    │
  │   agent loop ── tools ──┬── Home Assistant│──▶ HA REST API
  │      │                  └── Google Calendar│──▶ Calendar API v3
  │      │                                    │
  │   router ── route → primary, fallback     │
  │      │      (from models.yaml)            │
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

Which model answers lives in **`models.yaml`** (see `harness/models.example.yaml`;
the live copy is in arr-stack), mounted as a ConfigMap:

```yaml
routes:
  chat:
    primary: anthropic/claude-opus-5     # ← change this line
    fallback: openai/gpt-4o
```

No code change, no rebuild, no new image. The same applies to speech: `stt:`
and `tts:` are model references with fallbacks, exactly like the chat routes.

Adding a vendor nobody has written an adapter for is one file plus one line in
`build_registry()`. If it speaks `/v1/chat/completions`, it is just the line —
`provider_openai.py` already covers OpenAI, OpenRouter, Groq, Mistral, DeepSeek
and Cloudflare Workers AI.

**Fallbacks are for availability, not cost.** When the primary rate-limits or
5xxs, the route's fallback answers, so the house keeps working through a vendor
outage. Point it at a *different vendor* than the primary. A 4xx is treated as
non-retryable — a malformed request would fail identically on the fallback.

## Layout

```
harness/app/               flat modules — deliberately not a nested package
  provider_base.py         canonical types every layer above speaks
  provider_anthropic.py    native Messages API (tool use, thinking, effort)
  provider_openai.py       /v1/chat/completions — 6 vendors, one adapter
  provider_google.py       Gemini generateContent
  gateway.py               Cloudflare AI Gateway transport
  llm.py                   registry + fallback router
  agent.py                 the tool loop
  tool_registry.py         JSON Schema tool descriptions + dispatch
  tool_homeassistant.py    entities, states, services, HA's own intent engine
  tool_calendar.py         Calendar API v3 over raw REST
  google_auth.py           refresh token → access token
  speech.py                STT and TTS, same route-and-fallback pattern
  memory.py                per-session history in SQLite
  main.py                  FastAPI surface
  static/                  the voice PWA
harness/tests/             28 tests, standard library only
harness/models.example.yaml  routing config reference
scripts/google_oauth.py    one-time Google consent → refresh token
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

## Running it locally

```bash
cp .env.example .env          # fill in at least one vendor key
set -a && . ./.env && set +a
pip install -r harness/requirements.txt
cd harness/app && uvicorn main:app --reload --port 8080
```

Then open http://localhost:8080. `localhost` is a secure context, so the
microphone works without HTTPS.

## Google Calendar

A service account cannot reach a personal calendar, so the harness uses a user
refresh token. Once, on a machine with a browser:

```bash
python3 scripts/google_oauth.py --client-id ... --client-secret ...
```

It prints the three values to put into the deployment secret. Setup steps for
the Google Cloud side are in the script's docstring.

## Tests

```bash
python harness/tests/test_harness.py
```

Covers the fallback router, the tool loop (including parallel calls, tool
failures and the iteration cap), history repair, and each adapter's wire
format. No network, no API keys, standard library only.
