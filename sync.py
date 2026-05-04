"""
EmergencyAPI -> Notion incident logger.

Polls EmergencyAPI for active WA DFES incidents, filters to Perth metro,
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
# Config
# ---------------------------------------------------------------------------

# Perth metro bounding box: min_lon, min_lat, max_lon, max_lat
# Roughly Two Rocks down to Mandurah, hills to coast.
PERTH_BBOX = "115.55,-32.85,116.35,-31.45"

# Filter to WA + bbox server-side to keep payloads small.
EMERGENCYAPI_URL = (
    "https://emergencyapi.com/api/v1/incidents"
    f"?state=WA&bbox={PERTH_BBOX}&limit=100"
)

NOTION_API = "https://api.notion.com/v1"
NOTION_VERSION = "2022-06-28"

EMERGENCYAPI_KEY = os.environ["EMERGENCYAPI_KEY"]
NOTION_TOKEN = os.environ["NOTION_TOKEN"]
NOTION_DB_ID = os.environ["NOTION_DB_ID"]

NOTION_HEADERS = {
    "Authorization": f"Bearer {NOTION_TOKEN}",
    "Notion-Version": NOTION_VERSION,
    "Content-Type": "application/json",
}

EMERGENCYAPI_HEADERS = {
    "Authorization": f"Bearer {EMERGENCYAPI_KEY}",
    "Accept": "application/geo+json",
}

# ---------------------------------------------------------------------------
# Shift roster
# ---------------------------------------------------------------------------
# 8-day repeating cycle. Anchor: Sun 3 May 2026 = D platoon day, A platoon night.
# Day shift = 08:00-18:00 Perth local. Night shift = 18:00-08:00 (next day).
# Incidents before 08:00 belong to the previous calendar day's night shift.

SHIFT_ANCHOR_DATE = datetime(2026, 5, 3).date()

SHIFT_CYCLE = [
    ("D", "A"),  # offset 0  -> Sun 3 May
    ("D", "A"),  # offset 1
    ("C", "D"),  # offset 2
    ("C", "D"),  # offset 3
    ("B", "C"),  # offset 4
    ("B", "C"),  # offset 5
    ("A", "B"),  # offset 6
    ("A", "B"),  # offset 7
]

PERTH_OFFSET = timedelta(hours=8)  # AWST, no DST


def shift_for_incident(start_iso: str | None) -> tuple[str | None, str | None]:
    if not start_iso:
        return (None, None)
    try:
        dt = datetime.fromisoformat(start_iso.replace("Z", "+00:00"))
    except ValueError:
        return (None, None)

    if dt.tzinfo is None:
        perth = dt
    else:
        perth = (dt.astimezone(timezone.utc) + PERTH_OFFSET).replace(tzinfo=None)

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
    day_p, night_p = SHIFT_CYCLE[offset]
    return (shift, day_p if shift == "Day" else night_p)


# ---------------------------------------------------------------------------
# EmergencyAPI fetch
# ---------------------------------------------------------------------------

def fetch_incidents() -> list[dict[str, Any]]:
    """Fetch and flatten EmergencyAPI incidents (already filtered to WA + bbox)."""
    r = requests.get(EMERGENCYAPI_URL, headers=EMERGENCYAPI_HEADERS, timeout=30)
    if r.status_code >= 400:
        print(f"EmergencyAPI {r.status_code}: {r.text[:500]}", file=sys.stderr)
    r.raise_for_status()
    raw = r.json()

    incidents = []
    for feature in raw.get("features", []):
        props = feature.get("properties", {}) or {}
        geom = feature.get("geometry", {}) or {}
        coords = geom.get("coordinates") or [None, None]
        lon, lat = (coords[0], coords[1]) if len(coords) >= 2 else (None, None)

        loc = props.get("location") or {}
        ts = props.get("timestamps") or {}
        details = props.get("details") or {}
        source = props.get("source") or {}

        # Only keep DFES incidents (skip DFES-WARN warnings if they sneak through)
        if source.get("agency") != "DFES":
            continue

        incidents.append({
            "id": str(feature.get("id") or ""),
            "title": props.get("title") or "",
            "eventType": props.get("eventType") or "",
            "status": props.get("status") or "",
            "warningLevel": props.get("warningLevel") or "",
            "severity": props.get("severity") or "",
            "address": loc.get("address") or "",
            "suburb": loc.get("suburb") or "",
            "lga": loc.get("lga") or "",
            "size": details.get("size") or "",
            "resources": details.get("resources"),
            "description": details.get("description") or "",
            "reported": ts.get("reported") or "",
            "updated": ts.get("updated") or "",
            "lat": lat,
            "lon": lon,
            "raw": props,
        })

    return [i for i in incidents if i["id"]]


# ---------------------------------------------------------------------------
# Notion helpers
# ---------------------------------------------------------------------------

def notion_request(method: str, path: str, payload: dict | None = None) -> dict:
    url = f"{NOTION_API}{path}"
    for attempt in range(3):
        r = requests.request(method, url, headers=NOTION_HEADERS, json=payload, timeout=30)
        if r.status_code == 429:
            time.sleep(2 ** attempt)
            continue
        if r.status_code >= 400:
            print(f"Notion {method} {path} -> {r.status_code}: {r.text}", file=sys.stderr)
        r.raise_for_status()
        return r.json()
    raise RuntimeError("Notion API exhausted retries")


def query_existing_pages() -> dict[str, str]:
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


def build_properties(incident: dict, *, first_seen_iso: str) -> dict:
    raw_str = json.dumps(incident["raw"])[:1900]
    props = {
        "Incident ID":   {"title":     [{"text": {"content": incident["id"]}}]},
        "Title":         {"rich_text": [{"text": {"content": incident["title"]}}]},
        "Event Type":    {"rich_text": [{"text": {"content": incident["eventType"]}}]},
        "Severity":      {"rich_text": [{"text": {"content": incident["severity"]}}]},
        "Warning Level": {"rich_text": [{"text": {"content": incident["warningLevel"]}}]},
        "Address":       {"rich_text": [{"text": {"content": incident["address"]}}]},
        "Suburb":        {"rich_text": [{"text": {"content": incident["suburb"]}}]},
        "LGA":           {"rich_text": [{"text": {"content": incident["lga"]}}]},
        "Size":          {"rich_text": [{"text": {"content": incident["size"]}}]},
        "Description":   {"rich_text": [{"text": {"content": incident["description"][:1900]}}]},
        "First Seen":    {"date": {"start": first_seen_iso}},
        "Last Seen":     {"date": {"start": datetime.now(timezone.utc).isoformat()}},
        "Coordinates":   {"rich_text": [{"text": {"content": f"{incident['lat']},{incident['lon']}"}}]},
        "Raw JSON":      {"rich_text": [{"text": {"content": raw_str}}]},
    }
    if incident["status"]:
        props["Status"] = {"select": {"name": incident["status"]}}
    if incident["resources"] is not None:
        props["Resources"] = {"number": incident["resources"]}
    if incident["reported"]:
        props["Reported"] = {"date": {"start": incident["reported"]}}
    if incident["updated"]:
        props["Updated"] = {"date": {"start": incident["updated"]}}

    shift, platoon = shift_for_incident(incident["reported"])
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
    data = notion_request("GET", f"/pages/{page_id}")
    fs = data.get("properties", {}).get("First Seen", {}).get("date") or {}
    return fs.get("start") or datetime.now(timezone.utc).isoformat()


def close_page(page_id: str) -> None:
    payload = {
        "properties": {
            "Status": {"select": {"name": "closed"}},
            "Closed At": {"date": {"start": datetime.now(timezone.utc).isoformat()}},
        }
    }
    notion_request("PATCH", f"/pages/{page_id}", payload)


# ---------------------------------------------------------------------------
# Main
# ---------------------------------------------------------------------------

def main() -> None:
    incidents = fetch_incidents()
    print(f"Fetched {len(incidents)} DFES incidents in Perth metro")

    existing = query_existing_pages()
    live_ids = {i["id"] for i in incidents}

    new_count = updated_count = closed_count = 0

    for incident in incidents:
        if incident["id"] in existing:
            page_id = existing[incident["id"]]
            first_seen = get_page_first_seen(page_id)
            update_page(page_id, incident, first_seen)
            updated_count += 1
        else:
            create_page(incident)
            new_count += 1

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
