"""
Tender finder for a public sector consultancy.

Pulls open tenders from Find a Tender, keeps the ones that fit a consultancy
like GCF, scores them, and splits them into two lists:
  1. New: published in the last NEW_DAYS days
  2. Closing soon: deadline within CLOSING_DAYS days

Standard library only. Run with:  python tenders.py
"""

import csv
import json
import time
import urllib.error
import urllib.parse
import urllib.request
from datetime import datetime, timedelta, timezone

# ---------------------------------------------------------------------------
# Settings you can change
# ---------------------------------------------------------------------------

LOOKBACK_DAYS = 30      # how far back to fetch (tenders stay open for weeks)
NEW_DAYS = 2            # "new" means published in the last 2 days
CLOSING_DAYS = 7        # "closing soon" means deadline within 7 days
MIN_DAYS_LEFT = 2       # hide tenders closing sooner than this (no time to bid)
MIN_VALUE = 10_000      # hide tenders below this value (blank values are kept)
MAX_VALUE = 500_000     # hide tenders above this value (blank values are kept)
EXCLUDE_HEALTH = True   # hide NHS and health board buyers
OUTPUT_FILE = "relevant tenders.csv"

# CPV codes checked against the official list
CPV_CODES = {
    "79400000": "Business and management consultancy",
    "79411000": "General management consultancy",
    "79411100": "Business development consultancy",
    "79412000": "Financial management consultancy",
    "79419000": "Evaluation consultancy",
    "66171000": "Financial consultancy",
    "71241000": "Feasibility study, advisory service, analysis",
    "79314000": "Feasibility study",
    "79311400": "Economic research",
    "79311410": "Economic impact assessment",
    "73220000": "Development consultancy",
    "71314300": "Energy efficiency consultancy",
    "90713000": "Environmental issues consultancy",
    "72224000": "Project management consultancy",
    "71410000": "Urban planning",
}

# Words that suggest a good fit even when the CPV code is off
KEYWORDS = [
    "business case",
    "outline business case",
    "full business case",
    "strategic outline case",
    "green book",
    "feasibility",
    "options appraisal",
    "economic impact",
    "economic appraisal",
    "regeneration",
    "levelling up",
    "net zero",
    "decarbonisation",
    "masterplan",
    "investment plan",
    "growth strategy",
    "town centre",
]

HEALTH_WORDS = ["nhs", "health board", "healthcare", "hospital"]

# First three characters of the region code -> region name
# Find a Tender uses both NUTS (UKx) and ITL (TLx) style codes
REGIONS = {
    "C": "North East",
    "D": "North West",
    "E": "Yorkshire and the Humber",
    "F": "East Midlands",
    "G": "West Midlands",
    "H": "East of England",
    "I": "London",
    "J": "South East",
    "K": "South West",
    "L": "Wales",
    "M": "Scotland",
    "N": "Northern Ireland",
}

API = "https://www.find-tender.service.gov.uk/api/1.0/ocdsReleasePackages"
NOTICE_URL = "https://www.find-tender.service.gov.uk/Notice/"


# ---------------------------------------------------------------------------
# 1. Fetch
# ---------------------------------------------------------------------------

def get_json(url):
    """Fetch a URL and return JSON, waiting and retrying if rate limited."""
    for attempt in range(5):
        try:
            request = urllib.request.Request(url, headers={"Accept": "application/json"})
            with urllib.request.urlopen(request, timeout=60) as response:
                return json.load(response)
        except urllib.error.HTTPError as error:
            if error.code in (429, 503):
                try:
                    wait = int(error.headers.get("Retry-After", 10))
                except ValueError:
                    wait = 10
                print(f"  Rate limited, waiting {wait}s...")
                time.sleep(wait)
                continue
            raise
        except urllib.error.URLError:
            print("  Connection problem, retrying in 10s...")
            time.sleep(10)
    raise RuntimeError("Gave up after too many retries")


def fetch_releases(since=None):
    """Fetch every notice updated since a given time (default: LOOKBACK_DAYS ago).
    since is a string like 2026-10-08T06:00:00"""
    if since is None:
        since = (datetime.now() - timedelta(days=LOOKBACK_DAYS)).strftime("%Y-%m-%dT00:00:00")
    # No stages filter: it misses most notices, so we sort by tag ourselves
    params = {"updatedFrom": since, "limit": 100}
    url = f"{API}?{urllib.parse.urlencode(params)}"

    releases = []
    page = 0
    seen = set()
    while url and url not in seen and page < 1000:
        seen.add(url)
        page += 1
        data = get_json(url)
        batch = data.get("releases", [])
        releases.extend(batch)
        if page % 25 == 0:
            print(f"  Page {page}: {len(releases)} notices so far")
        url = (data.get("links") or {}).get("next") if batch else None
        time.sleep(0.3)  # be polite to the API

    return releases


def latest_per_process(releases):
    """A tender can have several notices (updates). Keep the newest one."""
    latest = {}
    for release in releases:
        key = release.get("ocid") or release.get("id")
        newer = parse_date(release.get("date")) or datetime.min.replace(tzinfo=timezone.utc)
        current = parse_date(latest[key].get("date")) if key in latest else None
        if current is None or newer > current:
            latest[key] = release
    return list(latest.values())


# ---------------------------------------------------------------------------
# 2. Clean
# ---------------------------------------------------------------------------

def parse_date(text):
    if not text:
        return None
    try:
        parsed = datetime.fromisoformat(text.replace("Z", "+00:00"))
        if parsed.tzinfo is None:
            parsed = parsed.replace(tzinfo=timezone.utc)
        return parsed
    except ValueError:
        return None


def all_cpv_codes(tender):
    codes = []
    main = tender.get("classification", {}).get("id")
    if main:
        codes.append(main)
    for item in tender.get("items", []):
        for extra in item.get("additionalClassifications", []):
            if extra.get("id"):
                codes.append(extra["id"])
    return codes


def region_name(release):
    tender = release.get("tender", {})
    code = None
    for item in tender.get("items", []):
        for address in item.get("deliveryAddresses", []):
            code = address.get("region")
            if code:
                break
        if code:
            break
    if not code:
        for party in release.get("parties", []):
            if "buyer" in party.get("roles", []):
                code = party.get("address", {}).get("region")
    if not code or len(code) < 3:
        return "Not stated"
    return REGIONS.get(code[2].upper(), code)


def value_of(tender):
    amount = tender.get("value", {}).get("amount")
    if amount is None:
        lot_values = [lot.get("value", {}).get("amount") for lot in tender.get("lots", [])]
        lot_values = [v for v in lot_values if v is not None]
        amount = sum(lot_values) if lot_values else None
    return amount


def clean(release):
    tender = release.get("tender", {})
    return {
        "title": (tender.get("title") or "").strip(),
        "buyer": (release.get("buyer") or {}).get("name") or "Unknown buyer",
        "value": value_of(tender),
        "deadline": parse_date(tender.get("tenderPeriod", {}).get("endDate")),
        "published": parse_date(release.get("date")),
        "category": tender.get("classification", {}).get("description", ""),
        "cpv_codes": all_cpv_codes(tender),
        "type": tender.get("mainProcurementCategory", ""),
        "status": tender.get("status", ""),
        "region": region_name(release),
        "summary": (tender.get("description") or "").strip(),
        "link": NOTICE_URL + release.get("id", ""),
    }


# ---------------------------------------------------------------------------
# 3. Filter and score
# ---------------------------------------------------------------------------

def score(row):
    """Higher score means a better fit. 0 means not relevant."""
    points = 0
    reasons = []

    matched_cpv = [c for c in row["cpv_codes"] if c in CPV_CODES]
    if matched_cpv:
        points += 3
        reasons.append(CPV_CODES[matched_cpv[0]])

    title = row["title"].lower()
    summary = row["summary"].lower()
    for word in KEYWORDS:
        if word in title:
            points += 3
            reasons.append(f'"{word}" in title')
        elif word in summary:
            points += 1
            reasons.append(f'"{word}" in summary')

    if "evaluation" in title:
        points += 3
        reasons.append('"evaluation" in title')

    return points, reasons


def keep(row, now):
    if row["status"] and row["status"] != "active":
        return False
    if row["type"] and row["type"] != "services":
        return False
    if row["deadline"] is None or row["deadline"] < now + timedelta(days=MIN_DAYS_LEFT):
        return False
    if row["value"] is not None and not (MIN_VALUE <= row["value"] <= MAX_VALUE):
        return False
    if EXCLUDE_HEALTH and any(w in row["buyer"].lower() for w in HEALTH_WORDS):
        return False
    return True


# ---------------------------------------------------------------------------
# 4. Show and save
# ---------------------------------------------------------------------------

def money(value):
    return "Not stated" if value is None else f"£{value:,.0f}"


def show(title, rows):
    print(f"\n{title} ({len(rows)})")
    print("-" * len(title))
    if not rows:
        print("Nothing today.")
    for row in rows:
        print(f"\n{row['title']}")
        print(f"  {row['buyer']} | {row['region']} | {money(row['value'])}")
        print(f"  Closes {row['deadline']:%d %b %Y} ({row['days_left']} days left)")
        print(f"  Why: {', '.join(row['reasons'])}")
        print(f"  {row['link']}")


def save(rows):
    fields = ["list", "score", "title", "buyer", "region", "value",
              "deadline", "days_left", "category", "why", "link"]
    with open(OUTPUT_FILE, "w", newline="", encoding="utf-8-sig") as f:
        writer = csv.DictWriter(f, fieldnames=fields)
        writer.writeheader()
        for row in rows:
            writer.writerow({
                "list": row["list"],
                "score": row["score"],
                "title": row["title"],
                "buyer": row["buyer"],
                "region": row["region"],
                "value": "" if row["value"] is None else row["value"],
                "deadline": f"{row['deadline']:%Y-%m-%d}",
                "days_left": row["days_left"],
                "category": row["category"],
                "why": "; ".join(row["reasons"]),
                "link": row["link"],
            })


def is_open_tender(release):
    """True if the newest notice for this procurement is a live tender notice.
    If it has since been awarded or cancelled, the newest notice says so."""
    tags = release.get("tag", [])
    if "tenderCancellation" in tags:
        return False
    return "tender" in tags or "tenderUpdate" in tags


def find_tenders(releases):
    """Turn raw releases into scored, relevant rows. Used by the website later."""
    now = datetime.now(timezone.utc)
    rows = []
    dropped = {"not an open tender": 0, "failed filters": 0, "no match": 0}
    for release in latest_per_process(releases):
        if not is_open_tender(release):
            dropped["not an open tender"] += 1
            continue
        row = clean(release)
        if not keep(row, now):
            dropped["failed filters"] += 1
            continue
        points, reasons = score(row)
        if points == 0:
            dropped["no match"] += 1
            continue
        row["score"] = points
        row["reasons"] = reasons
        row["days_left"] = (row["deadline"] - now).days
        is_new = row["published"] and row["published"] >= now - timedelta(days=NEW_DAYS)
        row["list"] = "New" if is_new else "Open"
        if row["days_left"] <= CLOSING_DAYS:
            row["list"] = "Closing soon"
        rows.append(row)
    rows.sort(key=lambda r: (-r["score"], r["deadline"]))
    print("Dropped:", dropped)
    return rows


def main():
    print(f"Fetching tenders from the last {LOOKBACK_DAYS} days...")
    releases = fetch_releases()
    print(f"Fetched {len(releases)} notices")

    rows = find_tenders(releases)

    show("New", [r for r in rows if r["list"] == "New"])
    show("Closing soon", [r for r in rows if r["list"] == "Closing soon"])
    print(f"\nOther open and relevant: {sum(r['list'] == 'Open' for r in rows)} (see the CSV)")

    save(rows)
    print(f"Saved {len(rows)} tenders to '{OUTPUT_FILE}'")


if __name__ == "__main__":
    main()
