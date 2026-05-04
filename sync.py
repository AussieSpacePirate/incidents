"""
DFES → Notion incident logger.

Polls the EmergencyWA active-incidents feed, filters to Perth metro,
and upserts each incident into a Notion database. Incidents that
disappear from the feed get their "Closed At" stamped on the next run.
"""

import os
import sys
import json
import time
from datetime import datetime, timedelta, timezone
from typing import Any

import requests

# ---------------------------------------------------------------------------
# Shift roster
# ---------------------------------------------------------------------------
# 8-day repeating cycle. Anchor: Sun 3 May 2026 = day 0 of cycle.
# That day was D platoon on day shift, A platoon on night shift.
# Verified against the May 2026 roster screenshot.
#
# Day shift = 08:00-18:00 Perth local.
# Night shift = 18:00-08:00 Perth local (rolls past midnight).
# An incident at e.g. 02:30 belongs to the PREVIOUS calendar day's night shift.

SHIFT_ANCHOR_DATE = datetime(2026, 5, 3).date()  # Sun 3 May 2026

# (day_platoon, night_platoon) for each offset 0..7 from the anchor
SHIFT_CYCLE = [
    ("D", "A"),  # offset 0  -> Sun 3 May
    ("D", "A"),  # offset 1  -> Mon 4 May
    ("C", "D"),  # offset 2  -> Tue 5 May
    ("C", "D"),  # offset 3  -> Wed 6 May
    ("B", "C"),  # offset 4  -> Thu 7 May
    ("B", "C"),  # offset 5  -> Fri 8 May
    ("A", "B"),  # offset 6  -> Sat 9 May
    ("A", "B"),  # offset 7  -> Sun 10 May
]

PERTH_OFFSET = timedelta(hours=8)  # AWST, no DST


def shift_for_incident(start_iso: str | None) -> tuple[str | None, str | None]:
    """
    Given an ISO-8601 start time (with offset), return (shift_name, platoon).
    Returns (None, None) if start_iso can't be parsed.
    """
    if not start_iso:
        return (None, None)
    try:
        dt = datetime.fromisoformat(start_iso)
    except ValueError:
        return (None, None)

    # Convert to Perth local
    if dt.tzinfo is None:
        perth = dt
    else:
        perth = (dt.astimezone(timezone.utc) + PERTH_OFFSET).replace(tzinfo=None)

    # Determine which shift window. Night shift owns 18:00 -> 08:00 next day,
    # so anything before 08:00 is yesterday's night shift.
    if perth.hour < 8:
        shift = "Night"
        roster_date = (perth - timedelta(days=1)).date()
    elif perth.hour < 18:
        shift = "Day"
        roster_date = perth.date()
    else:
        shift = "Night"
        roster_date = perth.date()

    offset = (roster_date - SHIFT_ANCHOR_DATE).days % 8
    day_platoon, night_platoon = SHIFT_CYCLE[offset]
    platoon = day_platoon if shift == "Day" else night_platoon
    return (shift, platoon)

# ---------------------------------------------------------------------------
# Config
# ---------------------------------------------------------------------------

DFES_URL = "https://www.emergency.wa.gov.au/data/incident_FCAD.json"
NOTION_API = "https://api.notion.com/v1"
NOTION_VERSION = "2022-06-28"

NOTION_TOKEN = os.environ["NOTION_TOKEN"]
NOTION_DB_ID = os.environ["NOTION_DB_ID"]

# Perth metro bounding box (approx). Tweak if you want a tighter/wider catchment.
# Covers roughly Two Rocks down to Mandurah, Perth Hills to the coast.
PERTH_BBOX = {
    "min_lat": -32.85,
    "max_lat": -31.45,
    "min_lon": 115.55,
    "max_lon": 116.35,
}

HEADERS = {
    "Authorization": f"Bearer {NOTION_TOKEN}",
    "Notion-Version": NOTION_VERSION,
    "Content-Type": "application/json",
}

# ---------------------------------------------------------------------------
# DFES fetch
# ---------------------------------------------------------------------------

def fetch_dfes_incidents() -> list[dict[str, Any]]:
    """Fetch and flatten the EmergencyWA incident feed."""
    r = requests.get(DFES_URL, timeout=30, headers={"User-Agent": "personal-archive/1.0"})
    r.raise_for_status()
    raw = r.json()

    incidents = []
    for feature in raw.get("features", []):
        props = feature.get("properties", {}) or {}
        geom = feature.get("geometry", {}) or {}
        coords = geom.get("coordinates") or [None, None]
        lon, lat = (coords[0], coords[1]) if len(coords) >= 2 else (None, None)

        incidents.append({
            "id": str(props.get("incidentEventsId") or props.get("id") or ""),
            "type": props.get("type") or "",
            "subType": props.get("subType") or "",
            "location": props.get("location") or "",
            "suburb": props.get("suburb") or "",
            "lga": props.get("lga") or "",
            "region": props.get("region") or "",
            "status": props.get("status") or "",
            "startTime": props.get("startTime") or "",
            "lastUpdatedTime": props.get("lastUpdatedTime") or "",
            "lat": lat,
            "lon": lon,
            "raw": props,
        })

    # Drop anything without an ID (we need it as the unique key)
    return [i for i in incidents if i["id"]]


def in_perth_metro(incident: dict) -> bool:
    lat, lon = incident["lat"], incident["lon"]
    if lat is None or lon is None:
        return False
    return (
        PERTH_BBOX["min_lat"] <= lat <= PERTH_BBOX["max_lat"]
        and PERTH_BBOX["min_lon"] <= lon <= PERTH_BBOX["max_lon"]
    )


# ---------------------------------------------------------------------------
# Notion helpers
# ---------------------------------------------------------------------------

def notion_request(method: str, path: str, payload: dict | None = None) -> dict:
    url = f"{NOTION_API}{path}"
    for attempt in range(3):
        r = requests.request(method, url, headers=HEADERS, json=payload, timeout=30)
        if r.status_code == 429:
            time.sleep(2 ** attempt)
            continue
        if r.status_code >= 400:
            print(f"Notion {method} {path} -> {r.status_code}: {r.text}", file=sys.stderr)
        r.raise_for_status()
        return r.json()
    raise RuntimeError("Notion API exhausted retries")


def query_existing_pages() -> dict[str, str]:
    """Return {incident_id: page_id} for every page currently in the database."""
    pages: dict[str, str] = {}
    cursor = None
    while True:
        payload = {"page_size": 100}
        if cursor:
            payload["start_cursor"] = cursor
        data = notion_request("POST", f"/databases/{NOTION_DB_ID}/query", payload)
        for page in data.get("results", []):
            props = page.get("properties", {})
            id_prop = props.get("Incident ID", {}).get("title", [])
            if id_prop:
                incident_id = id_prop[0].get("plain_text", "")
                if incident_id:
                    pages[incident_id] = page["id"]
        if not data.get("has_more"):
            break
        cursor = data.get("next_cursor")
    return pages


def parse_dt(value: str) -> str | None:
    """DFES timestamps look like '03/05/2026 14:32:11'. Convert to ISO-8601 with Perth offset."""
    if not value:
        return None
    for fmt in ("%d/%m/%Y %H:%M:%S", "%d/%m/%Y %H:%M", "%Y-%m-%dT%H:%M:%S"):
        try:
            dt = datetime.strptime(value, fmt)
            # Treat as Perth local time (UTC+8, no DST)
            return dt.strftime("%Y-%m-%dT%H:%M:%S+08:00")
        except ValueError:
            continue
    return None


def build_properties(incident: dict, *, first_seen_iso: str) -> dict:
    raw_str = json.dumps(incident["raw"])[:1900]  # Notion rich_text caps at 2000
    props = {
        "Incident ID": {"title": [{"text": {"content": incident["id"]}}]},
        "Type": {"rich_text": [{"text": {"content": incident["type"]}}]},
        "Sub Type": {"rich_text": [{"text": {"content": incident["subType"]}}]},
        "Location": {"rich_text": [{"text": {"content": incident["location"]}}]},
        "Suburb": {"rich_text": [{"text": {"content": incident["suburb"]}}]},
        "LGA": {"rich_text": [{"text": {"content": incident["lga"]}}]},
        "Status": {"select": {"name": incident["status"] or "Unknown"}} if incident["status"] else {"select": None},
        "First Seen": {"date": {"start": first_seen_iso}},
        "Last Seen": {"date": {"start": datetime.now(timezone.utc).isoformat()}},
        "Coordinates": {"rich_text": [{"text": {"content": f"{incident['lat']},{incident['lon']}"}}]},
        "Raw JSON": {"rich_text": [{"text": {"content": raw_str}}]},
    }
    start_iso = parse_dt(incident["startTime"])
    if start_iso:
        props["Start Time"] = {"date": {"start": start_iso}}

    shift, platoon = shift_for_incident(start_iso)
    if shift:
        props["Shift"] = {"select": {"name": shift}}
    if platoon:
        props["Platoon"] = {"select": {"name": platoon}}

    return props


def create_page(incident: dict) -> None:
    now_iso = datetime.now(timezone.utc).isoformat()
    payload = {
        "parent": {"database_id": NOTION_DB_ID},
        "properties": build_properties(incident, first_seen_iso=now_iso),
    }
    notion_request("POST", "/pages", payload)


def update_page(page_id: str, incident: dict, first_seen_iso: str) -> None:
    payload = {"properties": build_properties(incident, first_seen_iso=first_seen_iso)}
    notion_request("PATCH", f"/pages/{page_id}", payload)


def get_page_first_seen(page_id: str) -> str:
    """Read back the existing First Seen so we don't overwrite it."""
    data = notion_request("GET", f"/pages/{page_id}")
    fs = data.get("properties", {}).get("First Seen", {}).get("date") or {}
    return fs.get("start") or datetime.now(timezone.utc).isoformat()


def close_page(page_id: str) -> None:
    payload = {
        "properties": {
            "Status": {"select": {"name": "Closed"}},
            "Closed At": {"date": {"start": datetime.now(timezone.utc).isoformat()}},
        }
    }
    notion_request("PATCH", f"/pages/{page_id}", payload)


# ---------------------------------------------------------------------------
# Main
# ---------------------------------------------------------------------------

def main() -> None:
    incidents = fetch_dfes_incidents()
    metro = [i for i in incidents if in_perth_metro(i)]
    print(f"Fetched {len(incidents)} total, {len(metro)} in Perth metro")

    existing = query_existing_pages()
    live_ids = {i["id"] for i in metro}

    new_count = updated_count = closed_count = 0

    for incident in metro:
        if incident["id"] in existing:
            page_id = existing[incident["id"]]
            first_seen = get_page_first_seen(page_id)
            update_page(page_id, incident, first_seen)
            updated_count += 1
        else:
            create_page(incident)
            new_count += 1

    # Anything in Notion that's NOT in the live feed AND doesn't already have
    # Closed At set should be marked closed. We only check pages without
    # Closed At to avoid re-stamping every run.
    for incident_id, page_id in existing.items():
        if incident_id in live_ids:
            continue
        data = notion_request("GET", f"/pages/{page_id}")
        closed_at = data.get("properties", {}).get("Closed At", {}).get("date")
        if closed_at:
            continue
        close_page(page_id)
        closed_count += 1

    print(f"New: {new_count}, Updated: {updated_count}, Closed: {closed_count}")


if __name__ == "__main__":
    main()
