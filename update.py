"""
Daily updater for the tender finder website.

First run: fetches the last 30 days.
Every run after that: fetches only what changed since the last run, so a
skipped day is picked up automatically next time.

Keeps two files in the data folder:
  open_tenders.json  every open tender we know about (the memory between runs)
  matches.json       the ones that fit, which the website reads
"""

import json
import os
from datetime import datetime, timedelta, timezone

import tenders

DATA_DIR = "data"
STATE_FILE = os.path.join(DATA_DIR, "open_tenders.json")
MATCHES_FILE = os.path.join(DATA_DIR, "matches.json")
OVERLAP_HOURS = 2      # refetch a little before the last run so nothing slips through
SUMMARY_LENGTH = 800   # trim long descriptions for the website


def to_json(row):
    """Turn a cleaned row into something JSON can store (dates become text)."""
    out = dict(row)
    for key in ("deadline", "published"):
        out[key] = row[key].isoformat() if row[key] else None
    return out


def from_json(row):
    out = dict(row)
    for key in ("deadline", "published"):
        out[key] = tenders.parse_date(row[key])
    return out


def load_state():
    if not os.path.exists(STATE_FILE):
        return {"last_run": None, "tenders": {}}
    with open(STATE_FILE, encoding="utf-8") as f:
        return json.load(f)


def save_json(path, data):
    os.makedirs(DATA_DIR, exist_ok=True)
    with open(path, "w", encoding="utf-8") as f:
        json.dump(data, f, ensure_ascii=False, indent=1)


def main():
    now = datetime.now(timezone.utc)
    today = now.strftime("%Y-%m-%d")
    state = load_state()
    stored = state["tenders"]

    # 1. Work out where to start fetching from
    if state["last_run"]:
        last = datetime.fromisoformat(state["last_run"]) - timedelta(hours=OVERLAP_HOURS)
        since = last.strftime("%Y-%m-%dT%H:%M:%S")
        print(f"Fetching notices since {since}...")
    else:
        since = None
        print(f"First run: fetching the last {tenders.LOOKBACK_DAYS} days...")

    releases = tenders.fetch_releases(since)
    print(f"Fetched {len(releases)} notices")

    # 2. Apply notices oldest first, so the newest notice for each tender wins.
    #    A tender notice adds or updates it; an award or cancellation removes it.
    releases.sort(key=lambda r: tenders.parse_date(r.get("date")) or now)
    added = removed = 0
    for release in releases:
        key = release.get("ocid") or release.get("id")
        if tenders.is_open_tender(release):
            row = tenders.clean(release)
            row["summary"] = row["summary"][:SUMMARY_LENGTH]
            row["first_seen"] = stored.get(key, {}).get("first_seen", today)
            if key not in stored:
                added += 1
            stored[key] = to_json(row)
        elif key in stored:
            del stored[key]
            removed += 1

    # 3. Drop anything past its deadline
    for key in [k for k, r in stored.items()
                if not r["deadline"] or tenders.parse_date(r["deadline"]) < now]:
        del stored[key]
        removed += 1

    print(f"Added {added}, removed {removed}, now tracking {len(stored)} open tenders")

    # 4. Score every open tender, so filter changes apply to old ones too
    matches = []
    for row in map(from_json, stored.values()):
        if not tenders.keep(row, now):
            continue
        points, reasons = tenders.score(row)
        if points == 0:
            continue
        matches.append({
            "title": row["title"],
            "buyer": row["buyer"],
            "region": row["region"],
            "value": row["value"],
            "deadline": row["deadline"].isoformat(),
            "first_seen": row["first_seen"],
            "category": row["category"],
            "score": points,
            "why": ", ".join(reasons),
            "summary": row["summary"],
            "link": row["link"],
        })
    matches.sort(key=lambda m: (m["first_seen"], m["score"]), reverse=True)

    state["last_run"] = now.isoformat()
    save_json(STATE_FILE, state)
    save_json(MATCHES_FILE, {"updated": now.isoformat(), "tenders": matches})
    print(f"Saved {len(matches)} matching tenders")


if __name__ == "__main__":
    main()
