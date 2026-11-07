import glob
import pandas as pd
import streamlit as st

st.set_page_config(page_title="RouteGap", page_icon="✈️")

# ---------- load all the daily flight files into one table ----------
files = sorted(glob.glob("data/legs/*/legs_*.csv"))
tables = []
for f in files:
    one_day = pd.read_csv(f)
    one_day["day"] = pd.to_datetime(f[-14:-4])     # the date is in the file name
    tables.append(one_day)
everything = pd.concat(tables, ignore_index=True)

# airline flights only
everything = everything[everything["airline"].notna()]

# times are saved in UTC; show them in Indian time (IST)
for col in ["wheels_off_utc", "touchdown_utc"]:
    everything[col] = pd.to_datetime(everything[col], utc=True, format="mixed")
    everything[col] = everything[col].dt.tz_convert("Asia/Kolkata")

# for the charts: only flights where we know both ends
df = everything[everything["grade"].isin(["complete", "complete_inferred"])].copy()
df["route"] = df["origin"] + "-" + df["dest"]

days = len(files)
first_day = files[0][-14:-4]      # "legs_2026-10-01.csv" -> "2026-10-01"
last_day = files[-1][-14:-4]

# ---------- page ----------
st.title("✈️ RouteGap")
st.write("Open data on Indian domestic flights, built from ADS-B signals.")
st.caption(f"Source: adsb.lol (ODbL) · {days} days, {first_day} to {last_day} · "
           f"{len(df)} flights with both ends known")

tab_overview, tab_report, tab_gaps, tab_map, tab_find, tab_plane = st.tabs(
    ["Overview", "Route report", "Route gaps", "Route map", "Find a flight", "Aircraft day"])

# =====================================================================
with tab_overview:

    # 1. flights per airline
    st.header("Flights per airline")
    st.bar_chart(df["airline"].value_counts())

    # 2. busiest routes
    st.header("Busiest routes")
    route_counts = df["route"].value_counts()
    st.bar_chart(route_counts.head(20))
    st.caption(f"Top 20 of {len(route_counts)} routes seen.")

    # 3. pick an airport and see where it flies
    st.header("Routes from an airport")
    airports = sorted(df["origin"].unique())
    airport = st.selectbox("Choose an airport", airports, index=airports.index("DEL"))
    from_airport = df[df["origin"] == airport]
    st.write(f"**{airport}**: {len(from_airport)} departures seen to "
             f"{from_airport['dest'].nunique()} airports")
    st.bar_chart(from_airport["dest"].value_counts())

    # 4. pick a route and see who flies it
    st.header("Who flies this route?")
    route = st.selectbox("Choose a route (type to search)", route_counts.index)
    on_route = df[df["route"] == route]
    share = on_route["airline"].value_counts()

    st.write(f"**{route}**: {len(on_route)} flights seen, flown by {len(share)} airline(s)")
    st.bar_chart(share)

    if len(share) == 1:
        st.info("Only one airline seen on this route so far.")
    if len(on_route) < 60:
        st.info(f"Only {len(on_route)} flights seen. Treat these numbers as rough.")

    # 5. what we can't see yet
    st.header("Coverage")
    st.write(
        "We only count a flight when a volunteer ADS-B receiver saw it near both airports. "
        "Big airports in the south and west (BOM, BLR, HYD, MAA) are well covered. "
        "Some airports, like Indore (IDR), have no receiver nearby yet, so their flights "
        "don't appear here. That doesn't mean they aren't flying."
    )
    st.caption(f"Airports seen so far: {df['origin'].nunique()}")

# =====================================================================
with tab_report:

    st.header("Route report")
    st.write("Pick a route to see which airlines fly it, how punctual each one has been, "
             "and how they compare.")

    RATING_MIN = 20       # flights we must have matched before giving an airline a rating
    DGCA_LATE = 15        # DGCA counts a flight late if it leaves the gate 15+ min after schedule

    matched_files = sorted(glob.glob("data/matched/*/matched_*.csv"))
    if not matched_files:
        st.info("Punctuality data starts once schedules have been matched. Check back soon.")
    else:
        m = pd.concat([pd.read_csv(f) for f in matched_files], ignore_index=True)
        m["route"] = m["origin"] + " → " + m["dest"]
        sched_days = len(matched_files)

        # We see wheels-off, not pushback. Taxiing takes longer at some airports, so each
        # airport gets a taxi allowance: the delay of its fastest 20% of takeoffs (roughly
        # the flights that left the gate on time), kept between 5 and 25 minutes.
        ok_all = m[m["result"] == "matched"]
        taxi = ok_all.groupby("origin")["runway_delay_min"].quantile(0.2).clip(5, 25).round()

        # busiest routes first in the picker
        routes = m["route"].value_counts().index
        pick = st.selectbox("Route (type to search, e.g. BOM → BLR)", routes, key="report_route")
        r = m[m["route"] == pick]
        done = r[r["result"] == "matched"]
        o, d = pick.split(" → ")
        allowance = taxi.get(o, 15)

        st.subheader(pick)
        c1, c2, c3 = st.columns(3)
        c1.metric("Scheduled flights a day", f"{len(r) / sched_days:.0f}")
        c2.metric("Airlines", r["airline"].nunique())
        c3.metric("Flights we could check", f"{len(done) / len(r):.0%}",
                  help="Share of scheduled flights we matched to a real ADS-B flight. "
                       "The rest weren't seen by a receiver; that doesn't mean they didn't fly.")

        # typical time in the air on this route, from flights where we saw both ends
        seen = everything[(everything["origin"] == o) & (everything["dest"] == d)
                          & (everything["grade"] == "complete")]
        air_min = ((seen["touchdown_utc"] - seen["wheels_off_utc"]).dt.total_seconds() / 60).median()

        rows = []
        for airline, a in r.groupby("airline"):
            ok = a[a["result"] == "matched"]
            late = ok["runway_delay_min"] - allowance        # delay after taking off taxi time
            row = {
                "Airline": airline,
                "Flights a day": round(len(a) / sched_days, 1),
                "Share of route (%)": round(len(a) / len(r) * 100),
                "Checked": len(ok),
                "On time (%)": round((late <= DGCA_LATE).mean() * 100) if len(ok) else None,
                "Typical delay (min)": max(round(late.median()), 0) if len(ok) else None,
                "Cancelled": int((a["result"] == "cancelled").sum()),
                "Rating": None,
            }
            # the rating: only with enough flights, and built from parts people can see
            if len(ok) >= RATING_MIN:
                on_time = (late <= DGCA_LATE).mean()                               # 0 to 1
                delay_score = min(max(1 - late.median() / 60, 0), 1)               # 0 min = 1, 60+ = 0
                frequency = len(a) / len(r)                                        # 0 to 1
                row["Rating"] = round(5 * (0.6 * on_time + 0.25 * delay_score + 0.15 * frequency), 1)
            rows.append(row)

        table = pd.DataFrame(rows).sort_values("Flights a day", ascending=False)
        for col in ["On time (%)", "Typical delay (min)"]:
            table[col] = table[col].astype("Int64")       # whole numbers, blank when unknown
        shown = table.copy()
        shown["Rating"] = [f"⭐ {x}" if pd.notna(x) else f"needs {RATING_MIN - c} more checked"
                           for x, c in zip(table["Rating"], table["Checked"])]
        st.dataframe(shown, hide_index=True)

        rated = table.dropna(subset=["Rating"])
        if len(rated) >= 2:
            best = rated.sort_values("Rating", ascending=False).iloc[0]
            st.success(f"Most punctual on {pick} so far: **{best['Airline']}** "
                       f"({best['On time (%)']}% on time, {best['Checked']} flights checked)")
        elif len(done):
            st.info(f"Ratings unlock once an airline has {RATING_MIN} checked flights on a route. "
                    f"We have {sched_days} day(s) of punctuality data so far, so check back as it grows.")

        if pd.notna(air_min):
            st.caption(f"Typical time in the air: about {air_min:.0f} min "
                       f"(from {len(seen)} flights we saw take off and land).")

        with st.expander("How we work this out"):
            st.write(
                f"**On time** = left within {DGCA_LATE} minutes of the scheduled departure, the same "
                "line DGCA uses. We can't see pushback, only when the wheels leave the runway, so we "
                f"subtract a taxi allowance for the departure airport ({o}: {allowance:.0f} min). "
                "The allowance is the delay of that airport's fastest 20% of takeoffs, kept between "
                "5 and 25 minutes, so treat on-time numbers as estimates.\n\n"
                "Because every airline on a route uses the same airports, **comparing airlines on "
                "the same route is fairer than comparing raw numbers across routes.**\n\n"
                "**Typical delay** = the middle value (median) of the delay after taxi.\n\n"
                f"**Rating** (out of 5), only once an airline has {RATING_MIN}+ checked flights on "
                "the route: 60% on-time share, 25% typical delay (0 min scores full, 60+ scores "
                "zero), 15% how much of the route it flies.\n\n"
                "**Sources:** schedules from AeroDataBox, actual flights from adsb.lol (ODbL). "
                f"Punctuality covers {sched_days} day(s): {matched_files[0][-14:-4]} to "
                f"{matched_files[-1][-14:-4]}."
            )

# =====================================================================
with tab_gaps:

    st.header("Route gaps")
    st.write("Routes mostly served by a single airline. More airlines on a route usually "
             "means more choice for travellers, so these are spots where there could be room "
             "for another option. Busiest routes first.")

    # for every route: total flights, and how many each airline flew
    per_airline = df.groupby(["route", "airline"]).size().reset_index(name="flights")
    totals = per_airline.groupby("route")["flights"].sum()
    top = per_airline.sort_values("flights", ascending=False).drop_duplicates("route")
    top = top.set_index("route")

    gaps = pd.DataFrame({
        "Route": totals.index,
        "Flights seen": totals.values,
        "Main airline": top.loc[totals.index, "airline"].values,
        "Main airline's share (%)": (top.loc[totals.index, "flights"].values / totals.values * 100).round(),
        "Airlines seen": per_airline.groupby("route").size().loc[totals.index].values,
    })
    gaps["Flights per day"] = (gaps["Flights seen"] / days).round(1)

    c1, c2 = st.columns(2)
    kind = c1.radio("Show", ["Served by one airline", "Mostly one airline (80%+)"])
    min_flights = c2.number_input("At least this many flights", min_value=1, value=days)

    if kind == "Served by one airline":
        shown = gaps[gaps["Airlines seen"] == 1]
    else:
        shown = gaps[gaps["Main airline's share (%)"] >= 80]
    shown = shown[shown["Flights seen"] >= min_flights].sort_values("Flights seen", ascending=False)

    st.write(f"**{len(shown)} routes** match.")
    st.dataframe(shown, hide_index=True)

    if not shown.empty:
        st.bar_chart(shown["Main airline"].value_counts())
        st.caption("Which airlines serve these routes.")

    st.info("This is based on the flights we could see. At airports with weaker coverage we "
            "might miss some flights, so check the Route map tab for each airport's coverage.")
    st.caption(f"Default minimum = {days} flights, about one a day over the {days} days of data.")

# =====================================================================
with tab_map:

    st.header("Route map")
    st.write("Every route we saw. Thicker lines = more flights. "
             "Dots show how well we can see each airport.")

    airport_info = pd.read_csv("data/airports_india.csv")
    place = airport_info.set_index("iata")

    # --- the routes: count flights, then add both airports' positions ---
    lines = df.groupby(["origin", "dest"]).size().reset_index(name="flights")
    lines = lines[lines["origin"].isin(place.index) & lines["dest"].isin(place.index)]
    lines["from_lon"] = lines["origin"].map(place["lon"])
    lines["from_lat"] = lines["origin"].map(place["lat"])
    lines["to_lon"] = lines["dest"].map(place["lon"])
    lines["to_lat"] = lines["dest"].map(place["lat"])
    lines["width"] = lines["flights"] / lines["flights"].max() * 8 + 0.5

    min_flights = st.slider("Only show routes with at least this many flights", 1, 100, 10)
    lines = lines[lines["flights"] >= min_flights]

    # --- the airports: how was each end of each flight found there? ---
    ends = pd.concat([
        everything[["origin", "origin_method"]].set_axis(["iata", "method"], axis=1),
        everything[["dest", "dest_method"]].set_axis(["iata", "method"], axis=1),
    ]).dropna()
    seen = ends[ends["method"] == "seen"].groupby("iata").size()
    total = ends.groupby("iata").size()

    airport_info["flights"] = airport_info["iata"].map(total).fillna(0).astype(int)
    airport_info["seen_pct"] = (airport_info["iata"].map(seen).fillna(0)
                                / airport_info["flights"].clip(lower=1) * 100).round()

    def coverage(row):
        if row["flights"] == 0:
            return "No data"
        if row["seen_pct"] >= 70:
            return "Good"
        return "Partial"

    airport_info["coverage"] = airport_info.apply(coverage, axis=1)
    colours = {"Good": [34, 170, 90], "Partial": [240, 170, 30], "No data": [150, 150, 150]}
    airport_info["colour"] = airport_info["coverage"].map(colours)

    import pydeck as pdk

    route_layer = pdk.Layer(
        "LineLayer", lines,
        get_source_position=["from_lon", "from_lat"],
        get_target_position=["to_lon", "to_lat"],
        get_width="width", get_color=[60, 120, 220, 140], pickable=True,
    )
    airport_layer = pdk.Layer(
        "ScatterplotLayer", airport_info,
        get_position=["lon", "lat"], get_fill_color="colour",
        get_radius=18000, pickable=True,
    )
    st.pydeck_chart(pdk.Deck(
        map_style="light",
        layers=[route_layer, airport_layer],
        initial_view_state=pdk.ViewState(latitude=22, longitude=80, zoom=3.8),
        tooltip={"text": "{origin}-{dest}: {flights} flights\n{iata} {city}: {coverage}"},
    ))

    good = (airport_info["coverage"] == "Good").sum()
    partial = (airport_info["coverage"] == "Partial").sum()
    none = (airport_info["coverage"] == "No data").sum()
    st.write(f"🟢 **Good** ({good}): takeoffs and landings seen directly  ·  "
             f"🟠 **Partial** ({partial}): mostly estimated from climb/descent  ·  "
             f"⚪ **No data** ({none}): scheduled airport, no flights seen yet")
    st.caption(f"{len(lines)} routes shown. Airports: OurAirports list of {len(airport_info)} "
               "Indian airports with scheduled service.")

    with st.expander("Airports with no data"):
        st.dataframe(airport_info[airport_info["coverage"] == "No data"][["iata", "name", "city"]],
                     hide_index=True)

# =====================================================================
with tab_find:

    st.header("Find a flight")
    st.write("Search the flights we observed, by flight number, callsign or aircraft registration.")

    search_by = st.selectbox("Search by", ["Flight number", "Callsign", "Registration"])
    examples = {"Flight number": "e.g. 6E2134", "Callsign": "e.g. IGO2134", "Registration": "e.g. VT-IJK"}
    columns = {"Flight number": "flight_no", "Callsign": "callsign", "Registration": "reg"}

    text = st.text_input("Search", placeholder=examples[search_by])
    pick_date = st.checkbox("Pick a date")
    day = st.date_input("Date") if pick_date else None

    if text:
        # ignore spaces and capital letters: "6e 2134" finds "6E2134"
        wanted = text.replace(" ", "").upper()
        col = everything[columns[search_by]].fillna("").str.replace(" ", "").str.upper()
        found = everything[col == wanted]

        if day:
            found = found[found["day"].dt.date == day]

        if found.empty:
            st.warning("No flights found. We may not have seen it: some airports have no "
                       "receiver nearby, and our data starts on " + first_day + ".")
        else:
            st.success(f"{len(found)} flight(s) found")
            table = pd.DataFrame({
                "Date": found["day"],
                "Flight": found["flight_no"],
                "Airline": found["airline"],
                "From": found["origin"].fillna("?"),
                "To": found["dest"].fillna("?"),
                "Took off (IST)": found["wheels_off_utc"].dt.strftime("%H:%M"),
                "Landed (IST)": found["touchdown_utc"].dt.strftime("%H:%M"),
                "Aircraft": found["reg"].fillna("") + " " + found["type"].fillna(""),
                "Tracking": found["grade"].str.replace("_", " "),
            })
            table = table.sort_values("Date", ascending=False)
            table["Date"] = table["Date"].dt.strftime("%d %b %Y")
            table = table.fillna("not seen")
            st.dataframe(table, hide_index=True)
            st.caption("Times are when the aircraft left or reached the runway, not the gate. "
                       "'complete inferred' means one end was estimated from the climb or descent. "
                       "'?' or 'not seen' means no receiver saw that end of the flight.")

# =====================================================================
with tab_plane:

    st.header("Aircraft day")
    st.write("Pick one plane and one day, and see everything it flew.")

    # suggest the busiest planes so people have something to try
    busiest = everything["reg"].value_counts().head(5).index
    reg = st.text_input("Aircraft registration", placeholder="e.g. " + ", ".join(busiest))
    reg = reg.replace(" ", "").upper()

    if reg:
        plane = everything[everything["reg"].fillna("").str.upper() == reg]

        if plane.empty:
            st.warning("We haven't seen this aircraft. Check the registration, e.g. VT-IQG.")
        else:
            # which days did we see it?
            plane_days = sorted(plane["day"].dt.date.unique(), reverse=True)
            day = st.selectbox("Day", plane_days, format_func=lambda d: d.strftime("%a %d %b %Y"))
            legs = plane[plane["day"].dt.date == day].copy()

            # put the flights in time order (use takeoff time, or landing time if takeoff wasn't seen)
            legs["sort_time"] = legs["wheels_off_utc"].fillna(legs["touchdown_utc"])
            timed = legs[legs["sort_time"].notna()].sort_values("sort_time")
            untimed = legs[legs["sort_time"].isna()]

            st.subheader(f"{reg} · {plane['type'].iloc[0]} · {plane['airline'].iloc[0]}")

            # the chain: COK → PNQ → ATQ
            stops = []
            for _, leg in timed.iterrows():
                origin = leg["origin"] if pd.notna(leg["origin"]) else "?"
                dest = leg["dest"] if pd.notna(leg["dest"]) else "?"
                if not stops or stops[-1] != origin:
                    stops.append(origin)
                if dest == origin:
                    stops.append("?")      # it went somewhere and came back, but we missed the stop
                stops.append(dest)
            if stops:
                st.markdown("### " + " → ".join(stops))

            # hours in the air: only for flights where we saw both takeoff and landing
            both = timed[timed["wheels_off_utc"].notna() & timed["touchdown_utc"].notna()
                         & (timed["origin"] != timed["dest"])]
            air_minutes = ((both["touchdown_utc"] - both["wheels_off_utc"]).dt.total_seconds() / 60).sum()

            # turnaround: time on the ground between landing and the next takeoff at the same airport
            turns = []
            rows = list(timed.itertuples())
            for before, after in zip(rows, rows[1:]):
                same_airport = before.dest == after.origin and pd.notna(before.dest)
                if same_airport and pd.notna(before.touchdown_utc) and pd.notna(after.wheels_off_utc):
                    gap = (after.wheels_off_utc - before.touchdown_utc).total_seconds() / 60
                    if 0 < gap < 6 * 60:
                        turns.append(gap)

            c1, c2, c3 = st.columns(3)
            c1.metric("Flights seen", len(legs))
            c2.metric("Hours in the air", f"{air_minutes / 60:.1f}",
                      help=f"Only counts the {len(both)} flight(s) where we saw both takeoff and landing.")
            c3.metric("Average turnaround",
                      f"{sum(turns) / len(turns):.0f} min" if turns else "n/a",
                      help="Runway landing to next runway takeoff at the same airport.")

            table = pd.DataFrame({
                "Flight": timed["flight_no"],
                "From": timed["origin"].fillna("?"),
                "To": timed["dest"].fillna("?"),
                "Took off (IST)": timed["wheels_off_utc"].dt.strftime("%H:%M"),
                "Landed (IST)": timed["touchdown_utc"].dt.strftime("%H:%M"),
                "Minutes in air": ((timed["touchdown_utc"] - timed["wheels_off_utc"])
                                   .dt.total_seconds() / 60).round().astype("Int64").astype(str),
                "Tracking": timed["grade"].str.replace("_", " "),
            }).fillna("not seen")
            table["Minutes in air"] = table["Minutes in air"].replace("<NA>", "not seen")
            same = timed["origin"] == timed["dest"]
            table.loc[same.values, "Minutes in air"] = "missed a stop"
            st.dataframe(table, hide_index=True)

            if len(untimed) > 0:
                st.info(f"Plus {len(untimed)} flight(s) seen only while cruising, so we don't know "
                        "where or when they started and ended.")
            st.caption("A 'day' is one adsb.lol file: midnight to midnight UTC (5:30 AM to 5:30 AM IST). "
                       "Times are runway times, not gate times.")
