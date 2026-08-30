# home-harness

An **OpenAPI tool server** that lets an LLM control Home Assistant and manage
a Google Calendar.

It is deliberately small. [Open WebUI](https://docs.openwebui.com) provides the
chat interface, conversation storage, authentication, voice in and out, and
model routing — all of it better than a hand-rolled version would. This server
provides the one thing it cannot: the house.

```
  Open WebUI
    │  UI · sessions · auth · voice · model routing
    │
    ├─ models ──▶ Cloudflare AI Gateway ──▶ Anthropic · OpenAI · Gemini · …
    │
    └─ tools  ──▶ home-harness  (this repo)
                    ├─ Home Assistant   read state, call services, intents
                    └─ Google Calendar  read, create, update, delete
```

Open WebUI reads the OpenAPI spec this app publishes and turns **every
operation into a tool**. No plugin API, no SDK, no lock-in: any client that
speaks OpenAPI can use it.

CI publishes `ghcr.io/franion03/home-harness:latest`. Deployment lives in
[arr-stack](https://github.com/Franion03/arr-stack) under `apps/assistant/`.

## Registering it

Open WebUI → **Settings → Tools → +** and add the server URL:

```
http://harness.assistant.svc.cluster.local
```

with `TOOLS_API_KEY` as the Bearer token. Admins can add it under
*Settings → Admin → Integrations* to share it with every user.

## The tools

| Operation | Method | What it does |
|---|---|---|
| `list_home_entities` | `GET /ha/entities` | search devices and sensors, with state |
| `get_home_entity_state` | `GET /ha/entities/{id}` | one entity in full |
| `control_home_device` | `POST /ha/service` | turn things on/off, set values, run scenes |
| `ask_home_assistant` | `POST /ha/conversation` | hand a phrase to HA's own intent engine |
| `list_calendar_events` | `GET /calendar/events` | what is scheduled |
| `create_calendar_event` | `POST /calendar/events` | add an event |
| `update_calendar_event` | `PATCH /calendar/events/{id}` | change an event |
| `delete_calendar_event` | `DELETE /calendar/events/{id}` | remove an event |
| `list_calendars` | `GET /calendar/calendars` | available calendars |

`GET /health` reports which integrations came up. It is excluded from the
spec, so it never appears as a tool.

Everything else requires `Authorization: Bearer $TOOLS_API_KEY` (or
`X-API-Key`). The spec itself is readable without one, because Open WebUI
fetches it before it has anywhere to put a key.

## The descriptions are the prompt

Every `summary`, `description` and field description in `main.py` is written
for the **model**, not for a human reader — they become the tool definitions
the model uses to decide whether and how to call something. That is why they
say things like *"Call this first whenever you do not already know an entity's
exact id"* and *"Always confirm with the user before calling this."*

The test suite enforces it: an operation with no `operationId`, no summary, a
description under 40 characters, or an undescribed parameter fails CI.

## Two details worth knowing

**Entity lists are summarised before they leave.** A full `/api/states` dump
on a real installation is tens of thousands of tokens and would swamp the
model's context. `list_home_entities` returns `entity_id`, friendly name and
state only, filtered to the domains an assistant actually needs, capped at
200. Noisy attributes (`icon`, `supported_features`, `*_modes`) are stripped
from single-entity reads for the same reason.

**Failures are legible.** An unreachable Home Assistant is a 503 saying so; a
5xx from it is a 502 saying the request was not applied; an unknown entity
answers with a sentence telling the model to search first. A bare traceback
would tell the model nothing.

## Google Calendar

A service account cannot reach a personal calendar, so this uses a user
refresh token. Once, on a machine with a browser:

```bash
python3 scripts/google_oauth.py --client-id ... --client-secret ...
```

It prints the three values to put in the deployment secret. The Google Cloud
setup steps are in the script's docstring.

## Running it locally

```bash
cp .env.example .env
set -a && . ./.env && set +a
pip install -r harness/requirements.txt
cd harness/app && uvicorn main:app --reload --port 8080
```

Then browse http://localhost:8080/docs for the interactive spec.

## Tests

```bash
python harness/tests/test_harness.py
```

33 tests, standard library plus the app's own dependencies. Home Assistant and
Google are stubbed with `httpx.MockTransport` — **no network, no
credentials**.

They cover two things: that the tools behave (filtering, attribute stripping,
payload shapes, percent-encoded calendar ids, token caching, degraded
configuration), and that **the OpenAPI contract holds** — unique operation
ids, described parameters, and all nine tools present even when a credential
is missing, so Open WebUI's registration never silently loses half of them.
