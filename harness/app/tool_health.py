"""Health tools -- read body metrics out of InfluxDB with Flux.

The data arrives phone-side: Huawei Health -> Health Sync -> Health Connect
-> the Home Assistant companion app -> Home Assistant's InfluxDB integration.
Nothing in this repo writes it; this only reads.

Home Assistant's InfluxDB integration writes one measurement per *unit*
("kg", "bpm", "steps"), a single field `value`, and tags `entity_id`,
`domain` and `friendly_name`. So the useful handle is `entity_id`, and it is
matched loosely here -- the exact ids depend on which Health Connect sensors
the phone happens to expose, which is not knowable from the server.
"""

from __future__ import annotations

import csv
import io
import json
import logging

import httpx

log = logging.getLogger("harness.tools.health")

# Flux string literals are double-quoted, and a stray quote or backslash in a
# model-supplied pattern would otherwise end the literal early.
_ESCAPE = str.maketrans({'"': r"\"", "\\": r"\\"})


class Health:
    def __init__(
        self,
        url: str,
        token: str,
        org: str,
        bucket: str,
        *,
        transport: httpx.AsyncBaseTransport | None = None,
    ):
        self._url = url.rstrip("/")
        self._token = token
        self._org = org
        self._bucket = bucket
        self._client = httpx.AsyncClient(timeout=30.0, transport=transport)

    @property
    def configured(self) -> bool:
        return bool(self._url and self._token)

    async def _query(self, flux: str) -> list[dict]:
        resp = await self._client.post(
            f"{self._url}/api/v2/query",
            params={"org": self._org},
            headers={
                "Authorization": f"Token {self._token}",
                "Content-Type": "application/vnd.flux",
                "Accept": "application/csv",
            },
            content=flux,
        )
        if resp.status_code >= 400:
            raise RuntimeError(
                f"InfluxDB query -> {resp.status_code}: {resp.text[:250]}"
            )
        rows: list[dict] = []
        for row in csv.DictReader(io.StringIO(resp.text)):
            # Influx CSV repeats the header between result tables and pads
            # every row with two empty leading columns.
            if not row.get("_value") or row.get("_value") == "_value":
                continue
            rows.append(row)
        return rows

    @staticmethod
    def _safe(pattern: str) -> str:
        return pattern.translate(_ESCAPE)

    # ---- tool implementations -----------------------------------------

    async def list_metrics(self, days: int = 30) -> str:
        flux = f'''
from(bucket: "{self._bucket}")
  |> range(start: -{max(1, days)}d)
  |> filter(fn: (r) => r["_field"] == "value")
  |> group(columns: ["entity_id", "_measurement"])
  |> count()
  |> group()
  |> sort(columns: ["_value"], desc: true)
  |> limit(n: 50)
'''
        rows = await self._query(flux)
        if not rows:
            return (
                f"No health data recorded in the last {days} days. The phone "
                "pipeline may not be delivering yet."
            )
        out = [
            {
                "entity_id": r.get("entity_id"),
                "unit": r.get("_measurement"),
                "points": int(r["_value"]),
            }
            for r in rows
        ]
        return json.dumps(out, ensure_ascii=False)

    async def read_metric(
        self,
        metric: str,
        days: int = 30,
        aggregate: str = "mean",
        every: str = "1d",
    ) -> str:
        agg = aggregate if aggregate in ("mean", "max", "min", "last", "sum") else "mean"
        flux = f'''
from(bucket: "{self._bucket}")
  |> range(start: -{max(1, days)}d)
  |> filter(fn: (r) => r["_field"] == "value")
  |> filter(fn: (r) => r["entity_id"] =~ /(?i){self._safe(metric)}/)
  |> aggregateWindow(every: {every}, fn: {agg}, createEmpty: false)
  |> keep(columns: ["_time", "_value", "entity_id"])
  |> sort(columns: ["_time"])
  |> limit(n: 200)
'''
        rows = await self._query(flux)
        if not rows:
            return (
                f"No readings matching '{metric}' in the last {days} days. Call "
                "list_health_metrics to see which metrics exist."
            )
        series = [
            {
                "time": r["_time"][:19],
                "value": round(float(r["_value"]), 2),
                "entity_id": r.get("entity_id"),
            }
            for r in rows
        ]
        values = [p["value"] for p in series]
        summary = {
            "metric": metric,
            "points": len(series),
            "first": series[0]["value"],
            "latest": series[-1]["value"],
            "min": min(values),
            "max": max(values),
            "average": round(sum(values) / len(values), 2),
            # The model should describe the trend, not recompute it.
            "change": round(series[-1]["value"] - series[0]["value"], 2),
            "series": series,
        }
        return json.dumps(summary, ensure_ascii=False)

    async def aclose(self) -> None:
        await self._client.aclose()
