"""
Tender finder website. Run locally with:  streamlit run app.py
Reads data/matches.json, which update.py refreshes every morning.
"""

import csv
import io
import json
import os
from datetime import datetime, timedelta, timezone

import streamlit as st

MATCHES_FILE = os.path.join("data", "matches.json")
NEW_DAYS = 2  # how long a tender keeps its "New" label

st.set_page_config(page_title="Tender Finder", page_icon="📋", layout="centered")


@st.cache_data(ttl=600)
def load(modified_time):
    with open(MATCHES_FILE, encoding="utf-8") as f:
        return json.load(f)


def money(value):
    return "Value not stated" if value is None else f"£{value:,.0f}"


def as_csv(rows):
    """Plain spreadsheet of the current list, ready to drop into Copilot."""
    buffer = io.StringIO()
    fields = ["title", "buyer", "region", "value", "deadline", "first_seen",
              "category", "why", "summary", "link"]
    writer = csv.DictWriter(buffer, fieldnames=fields, extrasaction="ignore")
    writer.writeheader()
    for row in rows:
        writer.writerow({**row, "deadline": row["deadline"][:10]})
    return buffer.getvalue().encode("utf-8-sig")


# ---------------------------------------------------------------------------

st.title("Tender Finder")

if not os.path.exists(MATCHES_FILE):
    st.info("No data yet. The first update runs tomorrow morning.")
    st.stop()

data = load(os.path.getmtime(MATCHES_FILE))
now = datetime.now(timezone.utc)
today = now.date()
updated = datetime.fromisoformat(data["updated"])
rows = data["tenders"]

for row in rows:
    deadline = datetime.fromisoformat(row["deadline"])
    row["days_left"] = (deadline - now).days
    row["closes"] = deadline.strftime("%a %d %b")
    row["is_new"] = (today - datetime.fromisoformat(row["first_seen"]).date()).days < NEW_DAYS
rows = [r for r in rows if r["days_left"] >= 0]

new_count = sum(r["is_new"] for r in rows)
st.caption(f"Updated {updated:%A %d %B, %H:%M} UTC · {len(rows)} open tenders that fit · {new_count} new")

# --- Controls ---------------------------------------------------------------

show = st.segmented_control(
    "Show",
    ["New", "Last 7 days", "Closing this week", "All open"],
    default="New",
)
search = st.text_input("Search", placeholder="e.g. business case, regeneration, Devon")
regions = sorted({r["region"] for r in rows})
picked_regions = st.multiselect("Region", regions, placeholder="All regions")

# --- Filter -----------------------------------------------------------------

week_ago = today - timedelta(days=7)
if show == "New":
    shown = [r for r in rows if r["is_new"]]
elif show == "Last 7 days":
    shown = [r for r in rows if datetime.fromisoformat(r["first_seen"]).date() >= week_ago]
elif show == "Closing this week":
    shown = sorted([r for r in rows if r["days_left"] <= 7], key=lambda r: r["deadline"])
else:
    shown = rows

if search:
    words = search.lower().split()
    shown = [r for r in shown
             if all(w in f"{r['title']} {r['buyer']} {r['region']} {r['summary']}".lower()
                    for w in words)]
if picked_regions:
    shown = [r for r in shown if r["region"] in picked_regions]

# --- Results ----------------------------------------------------------------

left, right = st.columns([3, 1])
left.subheader(f"{len(shown)} tender{'s' if len(shown) != 1 else ''}")
right.download_button(
    "Download list",
    as_csv(shown),
    file_name=f"tenders {today:%d %b %Y}.csv",
    mime="text/csv",
    disabled=not shown,
    use_container_width=True,
)

if not shown:
    if show == "New":
        st.write("Nothing new since the last update. Try **Last 7 days**.")
    else:
        st.write("Nothing matches. Try clearing the search or region.")

for row in shown:
    with st.container(border=True):
        label = ":green-badge[New] " if row["is_new"] else ""
        st.markdown(f"{label}**[{row['title']}]({row['link']})**")

        urgent = row["days_left"] <= 7
        closes = f"Closes {row['closes']} ({row['days_left']} days left)"
        st.markdown(
            f"{row['buyer']} · {row['region']} · {money(row['value'])} · "
            + (f":red[{closes}]" if urgent else closes)
        )
        with st.expander("Details"):
            st.caption(f"Why it matched: {row['why']}")
            st.write(row["summary"] or "No description given.")
