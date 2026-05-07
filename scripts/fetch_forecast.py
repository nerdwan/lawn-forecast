#!/usr/bin/env python3
"""
fetch_forecast.py — Build data files for the Leinster Mow Forecast site.

Outputs:
  data/hourly.json    Per-hour structured forecast (essential for the site).
  data/leinster.json  Regional prose forecast (today / tomorrow / outlook).

Data sources (Met Éireann open data):
  - Point forecast XML (metno locationforecast format)
      http://openaccess.pf.api.met.ie/metno-wdb2ts/locationforecast?lat=...;long=...
      Hourly out to ~90h, 3-hourly to ~144h, 6-hourly to ~240h.
  - Leinster regional text forecast XML (RSS-style)
      https://www.met.ie/Open_Data/xml/xLeinster.xml

Licence note: Met Éireann data is published under a CC BY 4.0 + custom
addendum that requires attribution AND display of weather warnings on any
public site that shows live forecast data. The mow forecast site shows
attribution; if you ever publish it more broadly, add a warnings widget.
"""

from __future__ import annotations

import json
import os
import sys
import xml.etree.ElementTree as ET
from datetime import datetime, timezone
from pathlib import Path
from urllib.parse import quote
from urllib.request import Request, urlopen

# ── Configuration ─────────────────────────────────────────────────────────
# Point forecast location. Default is Kells, Co. Meath — change to the
# coordinates you actually care about (e.g. your own garden).
LAT = float(os.environ.get("MOW_LAT", "53.7263"))
LON = float(os.environ.get("MOW_LON", "-6.8779"))
LOCATION_NAME = os.environ.get("MOW_LOCATION_NAME", "Kells, Co. Meath")

POINT_FORECAST_URL = (
    f"http://openaccess.pf.api.met.ie/metno-wdb2ts/locationforecast"
    f"?lat={LAT};long={LON}"
)
LEINSTER_XML_URL = "https://www.met.ie/Open_Data/xml/xLeinster.xml"

OUT_DIR = Path("data")
HOURLY_OUT = OUT_DIR / "hourly.json"
LEINSTER_OUT = OUT_DIR / "leinster.json"

USER_AGENT = "mow-forecast-bot/1.0 (github.com/your-repo; contact@example.com)"
HTTP_TIMEOUT = 30  # seconds


# ── HTTP helpers ──────────────────────────────────────────────────────────
def http_get(url: str) -> bytes:
    """GET with a real User-Agent so met.ie doesn't 403 us."""
    req = Request(url, headers={"User-Agent": USER_AGENT, "Accept": "application/xml,text/xml,*/*"})
    with urlopen(req, timeout=HTTP_TIMEOUT) as resp:
        return resp.read()


# ── Parsers ───────────────────────────────────────────────────────────────
def parse_iso(ts: str) -> datetime:
    """Parse an ISO-8601 timestamp from Met Éireann (always UTC, ends in Z)."""
    if ts.endswith("Z"):
        ts = ts[:-1] + "+00:00"
    return datetime.fromisoformat(ts)


def parse_point_forecast(xml_bytes: bytes) -> list[dict]:
    """
    Convert the metno locationforecast XML into a flat list of hourly entries.

    The format has two kinds of <time> blocks for any given timestamp:
      - Instant entries (from == to): temperature, wind, humidity, cloud, etc.
      - Period entries (from < to):   precipitation accumulated over the span.

    For each instant we want the precipitation accumulated during the
    *following* interval. Met Éireann emits 1h periods for the first ~48h,
    then 3h, then 6h. We pick the shortest period that starts at the
    instant's timestamp, and (for longer periods) spread the rain evenly.
    """
    root = ET.fromstring(xml_bytes)

    instants: dict[str, dict] = {}
    periods: list[tuple[str, str, float]] = []

    for time_el in root.iter("time"):
        t_from = time_el.get("from")
        t_to = time_el.get("to")
        loc = time_el.find("location")
        if loc is None or t_from is None or t_to is None:
            continue

        if t_from == t_to:
            # Instant — extract everything we can.
            entry: dict = {}
            temp = loc.find("temperature")
            if temp is not None and temp.get("value") is not None:
                entry["temp_c"] = float(temp.get("value"))

            wind = loc.find("windSpeed")
            if wind is not None:
                # Prefer mps; fall back to other attrs if absent.
                if wind.get("mps") is not None:
                    entry["wind_kmh"] = round(float(wind.get("mps")) * 3.6, 1)

            cloud = loc.find("cloudiness")
            if cloud is not None and cloud.get("percent") is not None:
                entry["cloud_pct"] = round(float(cloud.get("percent")), 1)

            hum = loc.find("humidity")
            if hum is not None and hum.get("value") is not None:
                entry["humidity_pct"] = round(float(hum.get("value")), 1)

            if entry:
                instants[t_from] = entry
        else:
            # Period — extract precipitation accumulation.
            precip = loc.find("precipitation")
            if precip is not None and precip.get("value") is not None:
                periods.append((t_from, t_to, float(precip.get("value"))))

    # For each period, calculate per-hour rate (so 3mm over 3h = 1mm/h).
    # Index periods by their start time so we can find the shortest one
    # that starts at any given instant.
    by_start: dict[str, list[tuple[str, str, float]]] = {}
    for t_from, t_to, mm in periods:
        by_start.setdefault(t_from, []).append((t_from, t_to, mm))

    def span_hours(p: tuple[str, str, float]) -> float:
        return (parse_iso(p[1]) - parse_iso(p[0])).total_seconds() / 3600

    hours: list[dict] = []
    for ts in sorted(instants.keys()):
        iv = instants[ts]
        candidates = by_start.get(ts, [])
        if not candidates:
            # No precipitation forecast for the interval starting at this
            # instant. Could be the very last instant in the file. Skip it
            # rather than emit hours with unknown rain.
            continue
        # Shortest interval first.
        candidates.sort(key=span_hours)
        shortest = candidates[0]
        per_hour_mm = round(shortest[2] / max(1.0, span_hours(shortest)), 2)

        # Build the hour entry. Defaults are conservative — high humidity
        # so the drying model errs damp if data is missing.
        hours.append({
            "time": ts,
            "temp_c":       round(iv.get("temp_c", 10.0), 1),
            "wind_kmh":     iv.get("wind_kmh", 0.0),
            "cloud_pct":    iv.get("cloud_pct", 80.0),
            "humidity_pct": iv.get("humidity_pct", 85.0),
            "precip_mm":    per_hour_mm,
        })

    return hours


def parse_leinster_forecast(xml_bytes: bytes) -> dict:
    """
    Parse Met Éireann's regional forecast XML into the
    {forecasts: [{regions: [...]}]} shape the front-end expects.

    The actual format is a flat <forecast region="..."> element with direct
    text children: <today>, <tonight>, <tomorrow>, <pollen>, <solar_uv>, plus
    <issued issued="..."/>. (Not RSS — much simpler than I'd assumed.)
    """
    root = ET.fromstring(xml_bytes)
    region: dict = {}

    # Issued time is on a self-closing element with an `issued` attribute.
    issued_el = root.find("issued")
    if issued_el is not None:
        issued_attr = issued_el.get("issued")
        if issued_attr:
            try:
                # Already ISO-8601 with offset (e.g. "2026-05-07T17:00:00+01:00").
                region["issued"] = parse_iso(
                    issued_attr.replace("+01:00", "+01:00")  # leave as-is
                ).isoformat()
            except Exception:
                region["issued"] = issued_attr

    # Map the direct child text elements we care about.
    for tag in ("today", "tonight", "tomorrow", "pollen", "solar_uv"):
        el = root.find(tag)
        if el is not None and el.text and el.text.strip():
            region[tag] = el.text.strip()

    return {"forecasts": [{"regions": [region]}]}


# ── Main ──────────────────────────────────────────────────────────────────
def main() -> int:
    OUT_DIR.mkdir(parents=True, exist_ok=True)
    failures: list[str] = []

    # ── Hourly (essential) ────────────────────────────────────────────────
    print(f"Fetching point forecast: {POINT_FORECAST_URL}", flush=True)
    try:
        xml_bytes = http_get(POINT_FORECAST_URL)
        hours = parse_point_forecast(xml_bytes)
        if not hours:
            raise ValueError("Parsed 0 hourly entries — XML format may have changed.")
        out = {
            "issued": datetime.now(timezone.utc).isoformat(),
            "location": {"lat": LAT, "long": LON, "name": LOCATION_NAME},
            "hours": hours,
        }
        HOURLY_OUT.write_text(json.dumps(out, indent=2))
        print(f"  ✓ Wrote {HOURLY_OUT} — {len(hours)} hourly entries "
              f"(through {hours[-1]['time']})", flush=True)
    except Exception as e:
        failures.append(f"hourly: {e}")
        print(f"  ✗ FAILED to fetch/parse hourly forecast: {e}", flush=True, file=sys.stderr)

    # ── Leinster prose (nice-to-have) ─────────────────────────────────────
    print(f"Fetching Leinster regional forecast: {LEINSTER_XML_URL}", flush=True)
    try:
        xml_bytes = http_get(LEINSTER_XML_URL)
        leinster = parse_leinster_forecast(xml_bytes)
        LEINSTER_OUT.write_text(json.dumps(leinster, indent=2))
        regions = leinster["forecasts"][0]["regions"][0]
        keys = ", ".join(k for k in regions.keys() if k != "issued") or "(empty)"
        print(f"  ✓ Wrote {LEINSTER_OUT} — sections: {keys}", flush=True)
    except Exception as e:
        # Non-fatal: site degrades gracefully without the prose card.
        failures.append(f"leinster: {e}")
        print(f"  ⚠ FAILED to fetch/parse Leinster prose (non-fatal): {e}",
              flush=True, file=sys.stderr)

    # Fail the workflow only if the essential hourly data didn't write.
    if not HOURLY_OUT.exists():
        print(f"\nFatal: {HOURLY_OUT} was not written. Failures: {failures}",
              file=sys.stderr)
        return 1

    if failures:
        print(f"\nCompleted with non-fatal warnings: {failures}", flush=True)
    else:
        print("\nAll data files updated successfully.", flush=True)
    return 0


if __name__ == "__main__":
    sys.exit(main())
