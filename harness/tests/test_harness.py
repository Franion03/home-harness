"""Tests for the tool server.

Home Assistant and Google are stubbed with httpx.MockTransport, so the whole
suite runs with no network and no credentials.

Two things are being protected here:

  1. behaviour — the tools do the right thing, and fail in a way the model
     can recover from;
  2. the OpenAPI spec — Open WebUI turns it into the tool definitions the
     model reads, so a missing operationId or description is a real defect,
     not a documentation nit.

    python harness/tests/test_harness.py
"""

from __future__ import annotations

import json
import os
import sys
import unittest
from pathlib import Path

APP = Path(__file__).resolve().parents[1] / "app"
sys.path.insert(0, str(APP))

import httpx
from fastapi.testclient import TestClient

os.environ.update(
    HA_URL="http://ha.test:8123",
    HA_TOKEN="ha-token",
    GOOGLE_CLIENT_ID="cid",
    GOOGLE_CLIENT_SECRET="csecret",
    GOOGLE_REFRESH_TOKEN="rtoken",
    GOOGLE_CALENDAR_ID="primary",
    TOOLS_API_KEY="test-key",
    TZ="Europe/Madrid",
)

import main
import tool_calendar
import tool_health
import tool_homeassistant
import tool_tasks
from config import Settings
from google_auth import GoogleAuth

AUTH = {"X-API-Key": "test-key"}

STATES = [
    {"entity_id": "light.kitchen", "state": "on",
     "attributes": {"friendly_name": "Kitchen", "brightness": 180, "icon": "mdi:bulb"}},
    {"entity_id": "light.hall", "state": "off",
     "attributes": {"friendly_name": "Hallway"}},
    {"entity_id": "sensor.outside_temp", "state": "12.4",
     "attributes": {"friendly_name": "Outside temperature", "unit_of_measurement": "°C"}},
    {"entity_id": "person.fran", "state": "home", "attributes": {"friendly_name": "Fran"}},
    # Not in INTERESTING_DOMAINS, so it must not appear in an unfiltered list.
    {"entity_id": "update.core", "state": "off", "attributes": {"friendly_name": "Core update"}},
]


class FakeHome:
    def __init__(self):
        self.requests: list[httpx.Request] = []
        self.fail_with: int | None = None

    def handler(self, request: httpx.Request) -> httpx.Response:
        self.requests.append(request)
        if self.fail_with:
            return httpx.Response(self.fail_with, text="home assistant said no")
        p = request.url.path
        if p == "/api/states":
            return httpx.Response(200, json=STATES)
        if p.startswith("/api/states/"):
            wanted = p.rsplit("/", 1)[-1]
            for s in STATES:
                if s["entity_id"] == wanted:
                    return httpx.Response(200, json={**s, "last_changed": "2026-08-30T10:00:00Z"})
            return httpx.Response(404, text="Entity not found")
        if p.startswith("/api/services/"):
            return httpx.Response(200, json=[{"entity_id": "light.kitchen", "state": "off"}])
        if p == "/api/conversation/process":
            return httpx.Response(200, json={
                "response": {"speech": {"plain": {"speech": "Turned off 3 lights"}}}
            })
        return httpx.Response(404, text=f"no stub for {p}")

    def body(self, path_suffix: str) -> dict:
        for r in reversed(self.requests):
            if r.url.path.endswith(path_suffix):
                return json.loads(r.content)
        raise AssertionError(f"no request ending in {path_suffix}")


class FakeGoogle:
    def __init__(self):
        self.requests: list[httpx.Request] = []
        self.events = [
            {"id": "ev1", "summary": "Dentist",
             "start": {"dateTime": "2026-09-02T18:00:00+02:00"},
             "end": {"dateTime": "2026-09-02T19:00:00+02:00"},
             "location": "Clinic"},
            {"id": "ev2", "summary": "Holiday", "start": {"date": "2026-09-05"},
             "end": {"date": "2026-09-06"}},
        ]
        self.tasks = [
            # Deliberately out of order and with one undated task, so the
            # sort in tool_tasks is actually exercised.
            {"id": "t2", "title": "Buy stamps", "status": "needsAction"},
            {"id": "t1", "title": "Renew insurance", "status": "needsAction",
             "due": "2026-09-04T00:00:00.000Z"},
        ]
        self.token_calls = 0

    def handler(self, request: httpx.Request) -> httpx.Response:
        self.requests.append(request)
        url = str(request.url)
        if "oauth2.googleapis.com/token" in url:
            self.token_calls += 1
            return httpx.Response(200, json={"access_token": "at-123", "expires_in": 3600})
        p = request.url.path
        if p.endswith("/users/@me/lists"):
            return httpx.Response(200, json={"items": [
                {"id": "@default", "title": "My Tasks"},
                {"id": "list2", "title": "Shopping"},
            ]})
        if "/tasks/v1/lists/" in p and p.endswith("/tasks"):
            if request.method == "GET":
                return httpx.Response(200, json={"items": self.tasks})
            body = json.loads(request.content)
            return httpx.Response(200, json={"id": "t-new", **body})
        if "/tasks/v1/lists/" in p and request.method == "PATCH":
            return httpx.Response(200, json={"id": "t1", "title": "Renew insurance",
                                             "status": "completed"})
        if p.endswith("/calendarList"):
            return httpx.Response(200, json={"items": [
                {"id": "primary", "summary": "Fran", "primary": True, "accessRole": "owner"}
            ]})
        if p.endswith("/events") and request.method == "GET":
            return httpx.Response(200, json={"items": self.events})
        if p.endswith("/events") and request.method == "POST":
            body = json.loads(request.content)
            return httpx.Response(200, json={"id": "new1", **body})
        if request.method == "PATCH":
            return httpx.Response(200, json={"id": "ev1", "summary": "Moved",
                                             "start": {"dateTime": "2026-09-03T10:00:00"}})
        if request.method == "DELETE":
            return httpx.Response(204)
        return httpx.Response(404, text=f"no stub for {p}")

    def last(self, method: str) -> httpx.Request:
        for r in reversed(self.requests):
            if r.method == method and "oauth2" not in str(r.url):
                return r
        raise AssertionError(f"no {method} request")


class FakeInflux:
    """InfluxDB's /api/v2/query, answering in the CSV dialect it really uses."""

    HEADER = ",result,table,_start,_stop,_time,_value,_field,_measurement,entity_id"

    def __init__(self):
        self.requests: list[httpx.Request] = []
        self.rows = [
            ",_result,0,s,e,2026-08-25T00:00:00Z,82.4,value,kg,sensor.weight",
            ",_result,0,s,e,2026-08-31T00:00:00Z,81.0,value,kg,sensor.weight",
        ]

    def handler(self, request: httpx.Request) -> httpx.Response:
        self.requests.append(request)
        if request.url.path.endswith("/api/v2/query"):
            body = "\r\n".join([self.HEADER, *self.rows]) + "\r\n"
            return httpx.Response(200, text=body)
        return httpx.Response(404, text="no stub")

    @property
    def last_flux(self) -> str:
        return self.requests[-1].content.decode()


class ToolServerTest(unittest.TestCase):
    """Boots the app with both upstreams stubbed."""

    def setUp(self):
        self.home = FakeHome()
        self.google = FakeGoogle()

        s = main.Services.__new__(main.Services)
        s.settings = Settings()
        s.ha = tool_homeassistant.HomeAssistant(
            "http://ha.test:8123", "ha-token",
            transport=httpx.MockTransport(self.home.handler),
        )
        s.google_auth = GoogleAuth(
            "cid", "csecret", "rtoken",
            transport=httpx.MockTransport(self.google.handler),
        )
        s.calendar = tool_calendar.Calendar(
            s.google_auth, "primary", "Europe/Madrid",
            transport=httpx.MockTransport(self.google.handler),
        )
        s.tasks = tool_tasks.Tasks(
            s.google_auth, "@default", "Europe/Madrid",
            transport=httpx.MockTransport(self.google.handler),
        )
        self.influx = FakeInflux()
        s.health = tool_health.Health(
            "http://influx.test:8086", "influx-token", "casa", "health",
            transport=httpx.MockTransport(self.influx.handler),
        )
        main.services = s
        self.services = s
        self.client = TestClient(main.app)

    def tearDown(self):
        main.services = None


class TestOpenAPIContract(ToolServerTest):
    """The spec IS the tool definition Open WebUI hands to the model."""

    def setUp(self):
        super().setUp()
        self.spec = self.client.get("/openapi.json").json()

    def _operations(self):
        methods = {"get", "post", "put", "patch", "delete"}
        for path, item in self.spec["paths"].items():
            for method, op in item.items():
                if method in methods:
                    yield path, method, op

    def test_spec_is_openapi_3_x(self):
        # Open WebUI parses against the OpenAPI 3.x path-item model.
        self.assertTrue(self.spec["openapi"].startswith("3."))

    def test_every_operation_has_a_stable_unique_id(self):
        ids = [op["operationId"] for _, _, op in self._operations()]
        self.assertEqual(len(ids), len(set(ids)), "operationId must be unique")
        # Auto-generated ids are unreadable; these become the tool names.
        for i in ids:
            self.assertNotIn("__", i, f"{i} looks auto-generated")

    def test_every_operation_is_described_for_the_model(self):
        for path, method, op in self._operations():
            where = f"{method.upper()} {path}"
            self.assertTrue(op.get("summary"), f"{where} has no summary")
            self.assertGreater(
                len(op.get("description", "")), 40,
                f"{where} needs a description the model can act on",
            )

    def test_every_parameter_is_described(self):
        for path, method, op in self._operations():
            for param in op.get("parameters", []):
                self.assertTrue(
                    param.get("description"),
                    f"{method.upper()} {path} parameter {param['name']} has no description",
                )

    def test_request_body_fields_are_described(self):
        # FastAPI generates HTTPValidationError/ValidationError itself; only
        # the models we author become tool arguments.
        ours = {"ServiceCall", "Phrase", "NewEvent", "EventPatch"}
        for name, schema in self.spec.get("components", {}).get("schemas", {}).items():
            if name not in ours:
                continue
            for field, spec in (schema.get("properties") or {}).items():
                self.assertTrue(
                    spec.get("description"), f"{name}.{field} has no description"
                )

    def test_the_expected_tools_are_exposed(self):
        # An inventory, deliberately explicit: a tool appearing or vanishing
        # changes what the model can do, so it should never pass unnoticed.
        self.assertEqual(
            sorted(op["operationId"] for _, _, op in self._operations()),
            sorted([
                "ask_home_assistant", "control_home_device", "create_calendar_event",
                "delete_calendar_event", "get_home_entity_state", "list_calendar_events",
                "list_calendars", "list_home_entities", "update_calendar_event",
                "add_task", "complete_task", "list_task_lists", "list_tasks",
                "list_health_metrics", "read_health_metric",
            ]),
        )

    def test_health_is_not_offered_as_a_tool(self):
        self.assertNotIn("/health", self.spec["paths"])


class TestAuth(ToolServerTest):
    def test_health_is_open(self):
        r = self.client.get("/health")
        self.assertEqual(r.status_code, 200)
        self.assertTrue(r.json()["home_assistant"]["enabled"])

    def test_the_spec_is_readable_without_a_key(self):
        # Open WebUI fetches the spec before it has anywhere to put a key.
        self.assertEqual(self.client.get("/openapi.json").status_code, 200)

    def test_tools_require_a_key(self):
        self.assertEqual(self.client.get("/ha/entities").status_code, 401)

    def test_bearer_is_accepted(self):
        r = self.client.get("/ha/entities", headers={"Authorization": "Bearer test-key"})
        self.assertEqual(r.status_code, 200)

    def test_wrong_key_is_rejected(self):
        r = self.client.get("/ha/entities", headers={"X-API-Key": "nope"})
        self.assertEqual(r.status_code, 401)


class TestHomeAssistantTools(ToolServerTest):
    def test_listing_hides_noisy_domains_by_default(self):
        rows = json.loads(self.client.get("/ha/entities", headers=AUTH).json()["result"])
        ids = [r["entity_id"] for r in rows]
        self.assertIn("light.kitchen", ids)
        self.assertIn("person.fran", ids)
        # update.* is not in INTERESTING_DOMAINS.
        self.assertNotIn("update.core", ids)

    def test_domain_filter(self):
        rows = json.loads(
            self.client.get("/ha/entities?domain=light", headers=AUTH).json()["result"]
        )
        self.assertEqual({r["entity_id"].split(".")[0] for r in rows}, {"light"})

    def test_search_matches_the_friendly_name_not_just_the_id(self):
        rows = json.loads(
            self.client.get("/ha/entities?search=hallway", headers=AUTH).json()["result"]
        )
        self.assertEqual([r["entity_id"] for r in rows], ["light.hall"])

    def test_an_explicit_domain_can_reach_past_the_default_filter(self):
        rows = json.loads(
            self.client.get("/ha/entities?domain=update", headers=AUTH).json()["result"]
        )
        self.assertEqual([r["entity_id"] for r in rows], ["update.core"])

    def test_no_match_says_so_in_words_the_model_can_use(self):
        result = self.client.get("/ha/entities?search=zzz", headers=AUTH).json()["result"]
        self.assertIn("No matching entities", result)

    def test_state_strips_attributes_that_only_cost_tokens(self):
        state = json.loads(
            self.client.get("/ha/entities/light.kitchen", headers=AUTH).json()["result"]
        )
        self.assertEqual(state["state"], "on")
        self.assertEqual(state["attributes"]["brightness"], 180)
        self.assertNotIn("icon", state["attributes"])

    def test_an_unknown_entity_tells_the_model_how_to_recover(self):
        result = self.client.get("/ha/entities/light.nope", headers=AUTH).json()["result"]
        self.assertIn("No entity called", result)
        self.assertIn("list_entities", result.replace("ha_", ""))

    def test_calling_a_service_sends_the_right_payload(self):
        r = self.client.post("/ha/service", headers=AUTH, json={
            "domain": "light", "service": "turn_off", "entity_id": "light.kitchen",
        })
        self.assertEqual(r.status_code, 200)
        self.assertEqual(self.home.body("/turn_off"), {"entity_id": "light.kitchen"})

    def test_service_data_is_merged_into_the_payload(self):
        self.client.post("/ha/service", headers=AUTH, json={
            "domain": "light", "service": "turn_on", "entity_id": "light.kitchen",
            "data": {"brightness_pct": 40},
        })
        self.assertEqual(
            self.home.body("/turn_on"),
            {"entity_id": "light.kitchen", "brightness_pct": 40},
        )

    def test_conversation_returns_the_spoken_reply(self):
        r = self.client.post("/ha/conversation", headers=AUTH,
                             json={"text": "turn off everything downstairs"})
        self.assertEqual(r.json()["result"], "Turned off 3 lights")

    def test_an_upstream_failure_is_a_5xx_not_a_silent_success(self):
        self.home.fail_with = 500
        r = self.client.get("/ha/entities", headers=AUTH)
        self.assertGreaterEqual(r.status_code, 500)


class TestCalendarTools(ToolServerTest):
    def test_listing_summarises_events(self):
        rows = json.loads(
            self.client.get("/calendar/events", headers=AUTH).json()["result"]
        )
        self.assertEqual(rows[0]["summary"], "Dentist")
        self.assertEqual(rows[0]["id"], "ev1")
        self.assertTrue(rows[1]["all_day"])

    def test_the_window_is_sent_to_google(self):
        self.client.get("/calendar/events?days_ahead=3", headers=AUTH)
        params = self.google.last("GET").url.params
        self.assertEqual(params["singleEvents"], "true")
        self.assertEqual(params["orderBy"], "startTime")
        self.assertEqual(params["timeZone"], "Europe/Madrid")

    def test_creating_defaults_to_a_one_hour_event(self):
        r = self.client.post("/calendar/events", headers=AUTH, json={
            "summary": "Dentist", "start": "2026-09-02T18:00:00",
        })
        self.assertEqual(r.status_code, 200)
        body = json.loads(self.google.last("POST").content)
        self.assertEqual(body["start"]["dateTime"], "2026-09-02T18:00:00")
        self.assertEqual(body["end"]["dateTime"], "2026-09-02T19:00:00")

    def test_an_all_day_event_uses_dates_not_datetimes(self):
        self.client.post("/calendar/events", headers=AUTH, json={
            "summary": "Holiday", "start": "2026-09-05", "all_day": True,
        })
        body = json.loads(self.google.last("POST").content)
        self.assertEqual(body["start"], {"date": "2026-09-05"})
        self.assertNotIn("dateTime", body["start"])

    def test_updating_sends_only_the_changed_fields(self):
        self.client.patch("/calendar/events/ev1", headers=AUTH,
                          json={"summary": "Moved"})
        body = json.loads(self.google.last("PATCH").content)
        self.assertEqual(list(body), ["summary"])

    def test_updating_nothing_is_refused_rather_than_sent(self):
        r = self.client.patch("/calendar/events/ev1", headers=AUTH, json={})
        self.assertIn("Nothing to update", r.json()["result"])

    def test_deleting_reaches_google(self):
        r = self.client.delete("/calendar/events/ev1", headers=AUTH)
        self.assertEqual(r.status_code, 200)
        self.assertEqual(self.google.last("DELETE").method, "DELETE")

    def test_a_calendar_id_with_an_at_sign_is_percent_encoded(self):
        self.client.get(
            "/calendar/events?calendar_id=fran%40gmail.com", headers=AUTH
        )
        self.assertIn("fran%40gmail.com", str(self.google.last("GET").url))

    def test_the_access_token_is_reused_across_calls(self):
        self.client.get("/calendar/events", headers=AUTH)
        self.client.get("/calendar/calendars", headers=AUTH)
        self.assertEqual(self.google.token_calls, 1, "token should be cached")


class TestTaskTools(ToolServerTest):
    def test_listing_puts_the_soonest_due_first_and_undated_last(self):
        rows = json.loads(self.client.get("/tasks", headers=AUTH).json()["result"])
        self.assertEqual([r["id"] for r in rows], ["t1", "t2"])
        self.assertEqual(rows[0]["due"], "2026-09-04")
        self.assertIsNone(rows[1]["due"])

    def test_completed_tasks_are_hidden_unless_asked_for(self):
        self.client.get("/tasks", headers=AUTH)
        self.assertEqual(self.google.last("GET").url.params["showCompleted"], "false")
        self.client.get("/tasks?include_completed=true", headers=AUTH)
        params = self.google.last("GET").url.params
        # showHidden must accompany showCompleted or Google filters them out
        # again regardless.
        self.assertEqual(params["showCompleted"], "true")
        self.assertEqual(params["showHidden"], "true")

    def test_a_due_time_is_reduced_to_a_date(self):
        # Google Tasks stores no due time. Sending one and letting Google drop
        # it silently would have the model promise the user an hour.
        self.client.post("/tasks", headers=AUTH,
                         json={"title": "Renew insurance", "due": "2026-09-04T17:30:00"})
        body = json.loads(self.google.last("POST").content)
        self.assertEqual(body["due"], "2026-09-04T00:00:00.000Z")

    def test_adding_without_a_due_date_sends_none(self):
        self.client.post("/tasks", headers=AUTH, json={"title": "Buy stamps"})
        self.assertNotIn("due", json.loads(self.google.last("POST").content))

    def test_completing_patches_the_status(self):
        r = self.client.post("/tasks/t1/complete", headers=AUTH)
        self.assertEqual(r.status_code, 200)
        self.assertEqual(json.loads(self.google.last("PATCH").content),
                         {"status": "completed"})

    def test_task_lists_are_listed_with_ids(self):
        rows = json.loads(self.client.get("/tasks/lists", headers=AUTH).json()["result"])
        self.assertEqual(rows[0], {"id": "@default", "name": "My Tasks"})


class TestHealthTools(ToolServerTest):
    def test_reading_a_metric_summarises_the_trend(self):
        out = json.loads(
            self.client.get("/health-data/metric?metric=weight",
                            headers=AUTH).json()["result"]
        )
        self.assertEqual(out["points"], 2)
        self.assertEqual(out["latest"], 81.0)
        self.assertEqual(out["max"], 82.4)
        # The model should read the change, not recompute it from the series.
        self.assertEqual(out["change"], -1.4)

    def test_an_unknown_aggregate_falls_back_rather_than_breaking_flux(self):
        # A model may invent an aggregate name; an unchecked one would be
        # interpolated straight into the query and fail upstream.
        self.client.get("/health-data/metric?metric=weight&aggregate=median",
                        headers=AUTH)
        self.assertIn("fn: mean", self.influx.last_flux)

    def test_a_quote_in_the_pattern_cannot_escape_the_flux_literal(self):
        self.client.get('/health-data/metric?metric=we"ight', headers=AUTH)
        flux = self.influx.last_flux
        self.assertIn(r'we\"ight', flux)

    def test_no_data_is_reported_plainly_rather_than_as_an_error(self):
        self.influx.rows = []
        r = self.client.get("/health-data/metric?metric=weight", headers=AUTH)
        self.assertEqual(r.status_code, 200)
        self.assertIn("No readings matching", r.json()["result"])

    def test_listing_metrics_reports_the_pipeline_being_empty(self):
        self.influx.rows = []
        out = self.client.get("/health-data/metrics", headers=AUTH).json()["result"]
        self.assertIn("No health data", out)


class TestDegradedConfiguration(unittest.TestCase):
    """A missing credential must be a clear 503, not a crash at import."""

    def test_unconfigured_tools_report_why(self):
        s = main.Services.__new__(main.Services)
        s.settings = Settings()
        s.ha = None
        s.calendar = None
        s.google_auth = None
        main.services = s
        try:
            client = TestClient(main.app)
            r = client.get("/ha/entities", headers=AUTH)
            self.assertEqual(r.status_code, 503)
            self.assertIn("not configured", r.json()["detail"])

            r = client.get("/calendar/events", headers=AUTH)
            self.assertEqual(r.status_code, 503)

            # The spec must still list every tool, so Open WebUI's registration
            # does not silently lose half of them when a key is missing.
            # 12 paths carry 15 operations: /calendar/events is GET+POST,
            # /calendar/events/{id} is PATCH+DELETE, and /tasks is GET+POST.
            spec = client.get("/openapi.json").json()
            self.assertEqual(len(spec["paths"]), 12)
            ops = [m for i in spec["paths"].values() for m in i
                   if m in ("get", "post", "patch", "delete")]
            self.assertEqual(len(ops), 15)
        finally:
            main.services = None


if __name__ == "__main__":
    unittest.main(verbosity=2)
