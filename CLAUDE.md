# home-harness — notes for Claude

An OpenAPI tool server: Home Assistant + Google Calendar, for an LLM to call.

**Open WebUI is the assistant.** It owns the UI, sessions, auth, voice and
model routing. This repo owns the house. Deployment for both lives in
`Franion03/arr-stack` under `apps/assistant/`.

## The invariant that matters

**No model vendor, model id, API key or agent loop belongs in this repo.**

There is no provider abstraction here any more and there should not be one —
Open WebUI does that job. If a change wants to call an LLM, add a tool
instead and let the model decide when to use it. Earlier revisions of this
repo contained a full provider/router/agent-loop stack; it was removed
deliberately once Open WebUI took that role.

## The OpenAPI spec is the prompt

Open WebUI reads `/openapi.json` and turns every operation into a tool. So
`summary`, `description` and every `Field(description=...)` in `main.py` are
**model-facing prompt text**, not documentation. Write them as instructions:
when to call this, what to do first, what to confirm before acting.

`test_harness.py::TestOpenAPIContract` enforces this — an operation with no
`operationId`, no summary, a description under 40 characters, or an
undescribed parameter fails CI. Do not weaken those tests to make a change
pass; write the description.

Set `operation_id=` explicitly on every route. FastAPI's generated ids are
unreadable, and the id becomes the tool name the model sees.

## Things already handled — do not "fix" them

- **`list_entities` summarises and caps at 200.** A full `/api/states` dump is
  tens of thousands of tokens. Single-entity reads strip `icon`,
  `supported_features` and the `*_modes` lists for the same reason.
- **`INTERESTING_DOMAINS` filters the default listing**, but an explicit
  `domain=` still reaches past it — so nothing is truly hidden.
- **Exception handlers in `main.py` convert upstream failures into legible
  messages** (503 unreachable, 502 with "the request was not applied"). The
  model reads these and recovers; a bare traceback teaches it nothing.
- **Calendar ids are percent-encoded** — they are usually email addresses and
  land in a path segment.
- **`/openapi.json` is reachable without the API key.** Open WebUI fetches the
  spec before it has anywhere to put a key. `/health` is `include_in_schema=False`
  so it never becomes a tool.
- **`call_service` accepts `data` as a dict or a JSON string.** FastAPI gives a
  dict; a hand-written call may give a string.

## Tests

`python harness/tests/test_harness.py` — 33 tests, upstreams stubbed with
`httpx.MockTransport`. No network, no credentials. Do not add real API keys to
CI; nothing there needs them.

## Cluster facts (defined in arr-stack, not here)

- Namespace `assistant`. This service is ClusterIP-only with a NetworkPolicy
  admitting only Open WebUI — it can unlock doors and delete calendar events.
- Home Assistant is a separate box at `192.168.1.117:8123`, not in the cluster.
