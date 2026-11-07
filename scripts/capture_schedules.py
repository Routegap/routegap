"""
capture_schedules.py  -  RouteGap schedule capture

For each airport, asks AeroDataBox for yesterday's DEPARTURES (the airport's
departure board): when each flight was scheduled to leave and arrive, and when
it actually did. Saves one CSV per day:
    data/schedules/YYYY-MM/schedules_YYYY-MM-DD.csv

Runs every morning on GitHub Actions (.github/workflows/daily_schedules.yml).
Yesterday is used (not today) so the actual times are final.

Needs an API key in the environment variable AERODATABOX_KEY
(stored in GitHub: Settings -> Secrets and variables -> Actions).
Without a key it prints a message and stops, without failing.

Settings (environment variables, all optional):
    AIRPORTS           airports to capture, comma separated
                       (default: the 10 airports DGCA reports on-time performance for)
    DAY                which local date to capture, YYYY-MM-DD (default: yesterday in India)
    AERODATABOX_HOST   API host (default: aerodatabox.p.rapidapi.com, i.e. via RapidAPI)

Each airport-day costs 2 API calls (AeroDataBox allows at most 12 hours per call).

Usage:
    python scripts/capture_schedules.py
"""

import json
import os
import sys
import time
import urllib.error
import urllib.request
from datetime import date, datetime, timedelta, timezone
from pathlib import Path

import pandas as pd

ROOT = Path(__file__).resolve().parent.parent
OUT_DIR = ROOT / "data" / "schedules"

DGCA_AIRPORTS = "DEL,BOM,BLR,HYD,MAA,CCU,COK,AMD,LKO,GAU"
# "or" also covers settings that exist but are empty (GitHub passes blank variables as "")
AIRPORTS = (os.environ.get("AIRPORTS") or DGCA_AIRPORTS).replace(" ", "").split(",")
HOST = os.environ.get("AERODATABOX_HOST") or "aerodatabox.p.rapidapi.com"
KEY = os.environ.get("AERODATABOX_KEY") or ""

IST = timezone(timedelta(hours=5, minutes=30))


def ask_aerodatabox(airport, start, end):
    """One call: all departures from `airport` between two local times (max 12 hours apart)."""
    url = (f"https://{HOST}/flights/airports/iata/{airport}/"
           f"{start:%Y-%m-%dT%H:%M}/{end:%Y-%m-%dT%H:%M}"
           "?direction=Departure&withLeg=true&withCancelled=true"
           "&withCodeshared=false&withCargo=false&withPrivate=false")
    request = urllib.request.Request(url, headers={
        "X-RapidAPI-Key": KEY,
        "X-RapidAPI-Host": HOST,
    })
    for attempt in range(3):
        try:
            with urllib.request.urlopen(request, timeout=60) as response:
                if response.status == 204:          # no flights in this window
                    return []
                return json.load(response).get("departures", [])
        except urllib.error.HTTPError as error:
            if error.code == 429 and attempt < 2:    # too many requests: wait, then retry
                time.sleep(5 * (attempt + 1))
                continue
            print(f"  {airport} {start:%H:%M}-{end:%H:%M}: HTTP {error.code} {error.reason}")
            return []
    return []


def get(dictionary, *keys):
    """Safely read a nested value: get(f, 'departure', 'scheduledTime', 'utc')."""
    for key in keys:
        if not isinstance(dictionary, dict):
            return None
        dictionary = dictionary.get(key)
    return dictionary


def to_row(flight, airport):
    """Turn one flight from AeroDataBox into one row of our table."""
    dep = flight.get("departure") or flight.get("movement") or {}
    arr = flight.get("arrival") or {}
    number = (flight.get("number") or "").replace(" ", "").upper()
    return {
        "flight_no": number,                                   # e.g. 6E2134
        "callsign": flight.get("callSign"),
        "airline": get(flight, "airline", "name"),
        "airline_iata": get(flight, "airline", "iata"),
        "origin": get(dep, "airport", "iata") or airport,
        "dest": get(arr, "airport", "iata"),
        "sched_dep_utc": get(dep, "scheduledTime", "utc"),
        "revised_dep_utc": get(dep, "revisedTime", "utc"),
        "runway_dep_utc": get(dep, "runwayTime", "utc"),
        "sched_arr_utc": get(arr, "scheduledTime", "utc"),
        "revised_arr_utc": get(arr, "revisedTime", "utc"),
        "runway_arr_utc": get(arr, "runwayTime", "utc"),
        "status": flight.get("status"),
        "codeshare": flight.get("codeshareStatus"),
        "aircraft_reg": get(flight, "aircraft", "reg"),
        "aircraft_model": get(flight, "aircraft", "model"),
        "is_cargo": flight.get("isCargo"),
        "source": "AeroDataBox",
    }


def main():
    if not KEY:
        print("No AERODATABOX_KEY set, so no schedules captured. "
              "Add the key in GitHub: Settings -> Secrets and variables -> Actions.")
        return

    day = os.environ.get("DAY")
    day = date.fromisoformat(day) if day else datetime.now(IST).date() - timedelta(days=1)

    month_dir = OUT_DIR / f"{day:%Y-%m}"
    out_file = month_dir / f"schedules_{day}.csv"
    if out_file.exists():
        print(f"{out_file.name} already exists, nothing to do.")
        return

    # two 12-hour windows cover the whole local day
    midnight = datetime(day.year, day.month, day.day)
    windows = [(midnight, midnight + timedelta(hours=11, minutes=59)),
               (midnight + timedelta(hours=12), midnight + timedelta(hours=23, minutes=59))]

    rows = []
    for airport in AIRPORTS:
        before = len(rows)
        for start, end in windows:
            for flight in ask_aerodatabox(airport, start, end):
                rows.append(to_row(flight, airport))
            time.sleep(1)                                  # be gentle with the API
        print(f"{airport}: {len(rows) - before} departures")

    if not rows:
        print("Nothing returned. Check the key, the plan's limits, and the host.")
        sys.exit(1)

    table = pd.DataFrame(rows).drop_duplicates(["flight_no", "origin", "sched_dep_utc"])
    table["captured_at_utc"] = datetime.now(timezone.utc).strftime("%Y-%m-%dT%H:%MZ")
    month_dir.mkdir(parents=True, exist_ok=True)
    table.to_csv(out_file, index=False)
    print(f"Saved {len(table)} flights to {out_file.relative_to(ROOT)} "
          f"({2 * len(AIRPORTS)} API calls)")


if __name__ == "__main__":
    main()
