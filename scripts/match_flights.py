"""
match_flights.py  -  RouteGap: pair scheduled flights with the flights we saw

Inputs
    data/schedules/YYYY-MM/schedules_YYYY-MM-DD.csv   what was SCHEDULED (AeroDataBox)
    data/legs/YYYY-MM/legs_YYYY-MM-DD.csv             what we SAW fly (ADS-B)

Output, one file per schedule day:
    data/matched/YYYY-MM/matched_YYYY-MM-DD.csv

Every scheduled domestic flight gets one result:
    matched       we found it in the ADS-B data -> delay is known
    cancelled     AeroDataBox says it was cancelled
    not_observed  we didn't see it (coverage gap). Never "cancelled" - we can't tell.

How a scheduled flight finds its ADS-B flight (best clue first):
    1. same aircraft registration (e.g. VT-NHI)  + same departure airport + takeoff in the time window
    2. same flight number (e.g. 6E622)            + same departure airport + takeoff in the time window
    3. same airline + same route                  + nearest takeoff in the time window
Time window: from 30 min before the scheduled departure to 6 hours after.
Each ADS-B flight can be used only once.

Delay here = runway takeoff (wheels off) minus scheduled GATE departure, so it
includes taxi time (often 10-25 min). We call it "runway delay" and never
compare it directly with DGCA's on-time figures until taxi time is calibrated.

Usage:
    python scripts/match_flights.py            # every schedule day not matched yet
    python scripts/match_flights.py --force    # also redo days already matched
"""

import argparse
from datetime import date, timedelta
from pathlib import Path

import pandas as pd

ROOT = Path(__file__).resolve().parent.parent
SCHED_DIR = ROOT / "data" / "schedules"
LEGS_DIR = ROOT / "data" / "legs"
OUT_DIR = ROOT / "data" / "matched"
AIRPORTS = ROOT / "data" / "airports_india.csv"

EARLY_MIN = 30          # a takeoff up to 30 min before schedule still counts
LATE_MIN = 6 * 60       # ...and up to 6 hours after

# Airline names from the flight-number prefix. AeroDataBox's own names are
# sometimes wrong (it calls Akasa "Starlight Airline").
AIRLINES = {
    "6E": "IndiGo",
    "AI": "Air India",
    "IX": "Air India Express",
    "QP": "Akasa Air",
    "SG": "SpiceJet",
    "9I": "Alliance Air",
    "S5": "Star Air",
}


def legs_file(day):
    return LEGS_DIR / f"{day:%Y-%m}" / f"legs_{day}.csv"


def load_schedules(path):
    s = pd.read_csv(path)
    indian = set(pd.read_csv(AIRPORTS)["iata"])

    s["prefix"] = s["flight_no"].str[:2]
    s = s[s["prefix"].isin(AIRLINES)]                              # Indian airlines only
    s = s[s["origin"].isin(indian) & s["dest"].isin(indian)]       # domestic only
    s = s[s["is_cargo"] != True]
    s["airline"] = s["prefix"].map(AIRLINES)

    for col in ["sched_dep_utc", "sched_arr_utc", "revised_dep_utc", "runway_dep_utc"]:
        s[col] = pd.to_datetime(s[col], utc=True, errors="coerce")
    s = s.dropna(subset=["sched_dep_utc"])
    return s.reset_index(drop=True)


def load_legs(days):
    """ADS-B flights from several UTC days (an Indian day spans two UTC days)."""
    tables = [pd.read_csv(legs_file(d)) for d in days if legs_file(d).exists()]
    legs = pd.concat(tables, ignore_index=True)
    legs = legs[legs["airline"].notna()].copy()
    for col in ["wheels_off_utc", "touchdown_utc"]:
        legs[col] = pd.to_datetime(legs[col], utc=True, errors="coerce", format="mixed")
    legs = legs.dropna(subset=["wheels_off_utc"])                  # need a takeoff time
    legs["prefix"] = legs["flight_no"].str[:2]
    legs = legs.drop_duplicates(["hex", "wheels_off_utc"])         # same flight seen in two files
    return legs.reset_index(drop=True)


def could_be(airport, candidates):
    """True if `airport` is the leg's airport or one of its 'DEL|HDO' style candidates."""
    if pd.isna(candidates):
        return False
    return airport in str(candidates).split("|")


def match_day(sched, legs):
    """Returns the schedule table with the matched ADS-B flight added to each row."""
    used = set()
    found = {}            # schedule row -> (leg row, method)

    for method in ["registration", "flight_number", "route_and_time"]:
        pairs = []
        for i, flight in sched.iterrows():
            if i in found or flight["status"] == "Canceled":
                continue
            start = flight["sched_dep_utc"] - pd.Timedelta(minutes=EARLY_MIN)
            end = flight["sched_dep_utc"] + pd.Timedelta(minutes=LATE_MIN)
            window = legs[(legs["wheels_off_utc"] >= start) & (legs["wheels_off_utc"] <= end)]
            window = window[[could_be(flight["origin"], c) for c in window["origin_candidates"]]]

            if method == "registration":
                if pd.isna(flight["aircraft_reg"]):
                    continue
                window = window[window["reg"] == flight["aircraft_reg"]]
            elif method == "flight_number":
                window = window[window["flight_no"] == flight["flight_no"]]
            else:
                window = window[window["prefix"] == flight["prefix"]]
                # with no flight number or registration to go on, the destination must be
                # seen and must fit (without it, about half these matches were wrong)
                dest_ok = [could_be(flight["dest"], c) for c in window["dest_candidates"]]
                window = window[dest_ok]

            for j, leg in window.iterrows():
                gap = abs((leg["wheels_off_utc"] - flight["sched_dep_utc"]).total_seconds())
                pairs.append((gap, i, j))

        # closest in time first; each flight and each leg used once
        for gap, i, j in sorted(pairs):
            if i not in found and j not in used:
                found[i] = (j, method)
                used.add(j)

    out = sched.copy()
    out["match_method"] = None
    for col in ["hex", "reg", "type", "wheels_off_utc", "touchdown_utc", "grade",
                "origin_method", "dest_method"]:
        out["seen_" + col] = None
    for i, (j, method) in found.items():
        out.at[i, "match_method"] = method
        for col in ["hex", "reg", "type", "wheels_off_utc", "touchdown_utc", "grade",
                    "origin_method", "dest_method"]:
            out.at[i, "seen_" + col] = legs.at[j, col]

    out["result"] = "not_observed"
    out.loc[out["match_method"].notna(), "result"] = "matched"
    out.loc[out["status"] == "Canceled", "result"] = "cancelled"

    takeoff = pd.to_datetime(out["seen_wheels_off_utc"], utc=True, format="mixed")
    landing = pd.to_datetime(out["seen_touchdown_utc"], utc=True, format="mixed")
    out["runway_delay_min"] = ((takeoff - out["sched_dep_utc"]).dt.total_seconds() / 60).round()
    out["arrival_delay_min"] = ((landing - out["sched_arr_utc"]).dt.total_seconds() / 60).round()
    # an estimated end of the flight gives an estimated time - keep it, but say so
    out["times_estimated"] = (out["seen_origin_method"] == "inferred") | (out["seen_dest_method"] == "inferred")
    return out.drop(columns=["prefix"])


def report(day, out):
    total = len(out)
    matched = (out["result"] == "matched").sum()
    exact = out[(out["result"] == "matched") & ~out["times_estimated"]]
    print(f"{day}: {total} scheduled domestic flights · matched {matched} ({matched / total:.0%}) · "
          f"cancelled {(out['result'] == 'cancelled').sum()} · "
          f"not observed {(out['result'] == 'not_observed').sum()}")
    print("  by method:", out["match_method"].value_counts().to_dict())
    if len(exact):
        print(f"  runway delay, takeoff actually seen ({len(exact)} flights): "
              f"median {exact['runway_delay_min'].median():.0f} min")
    by_airport = out.groupby("origin")["result"].apply(lambda r: (r == "matched").mean() * 100).round()
    print("  matched % by departure airport:", by_airport.sort_values(ascending=False).astype(int).to_dict())


def main():
    parser = argparse.ArgumentParser(description="Match scheduled flights to ADS-B flights")
    parser.add_argument("--force", action="store_true", help="redo days that were already matched, "
                        "and match even if the next day's ADS-B data isn't in yet")
    args = parser.parse_args()

    for path in sorted(SCHED_DIR.glob("*/schedules_*.csv")):
        day = date.fromisoformat(path.stem[len("schedules_"):])
        out_file = OUT_DIR / f"{day:%Y-%m}" / f"matched_{day}.csv"
        if out_file.exists() and not args.force:
            continue

        # An Indian day runs 18:30 UTC the day before to 18:29 UTC, and late flights
        # can take off after midnight UTC, so we need ADS-B days D-1, D and D+1.
        needed = [day - timedelta(days=1), day, day + timedelta(days=1)]
        missing = [d for d in needed if not legs_file(d).exists()]
        if missing and not args.force:
            print(f"{day}: waiting for ADS-B data for {', '.join(map(str, missing))}")
            continue

        out = match_day(load_schedules(path), load_legs(needed))
        out_file.parent.mkdir(parents=True, exist_ok=True)
        out.to_csv(out_file, index=False)
        report(day, out)


if __name__ == "__main__":
    main()
