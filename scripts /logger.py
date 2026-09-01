# scripts/logger.py — General Stock Report 
#
# Same active/archive/re-age mechanic as the critical-news-logger.
# The difference: this logger receives RAW NARRATIVE TEXT (same
# format as generalstockreport.txt / your Google agent's output) and
# parses it itself via parser.py — nothing upstream needs to convert
# it to JSON first.
#
# Two ways a run can supply text:
#
#   1. Automated dispatch (dispatch_news_v2.py, adapted for this repo):
#      DISPATCH_CLIENT_PAYLOAD = {"raw_text": "<one story>"}
#                              or {"raw_text_list": ["<story1>", "<story2>", ...]}
#
#   2. Manual workflow_dispatch: paste the raw report text (one or more
#      '==='-delimited stories, exactly like your Google agent writes
#      it) into the "raw_text" form field.
#
# Either way, parser.parse_stories() splits on '===' and extracts the
# structured fields (see parser.py's own header comment for exactly
# what it can and can't reliably extract, and its stated limitations).
#
# A story that fails to parse does NOT block the ones that succeeded:
# this run still saves whatever DID parse, but exits with a non-zero
# code so the Action shows red and you can see which story failed and
# why in the run log. (The workflow's commit step runs with
# `if: always()` specifically so a partial failure doesn't also throw
# away the good data.)

import os
import sys
import json
from datetime import datetime, timedelta, timezone

# Add the scripts directory to Python path and import parser
sys.path.append(os.path.dirname(os.path.dirname(os.path.abspath(__file__))))
from scripts.parser import parse_stories

DATA_DIR = "data"
ACTIVE_FILE = os.path.join(DATA_DIR, "active_week.json")
ARCHIVE_FILE = os.path.join(DATA_DIR, "archive.json")

BUCKETS = ["past_2_weeks", "past_1_month", "past_3_months", "past_6_months", "past_1_year", "historical"]


def load_data(path, default_type=list):
    if os.path.exists(path):
        try:
            with open(path, 'r', encoding='utf-8') as f:
                return json.load(f)
        except Exception as e:
            print(f"Warning: Could not load {path}: {e}")
            return default_type()
    return default_type()


def save_data(path, obj):
    os.makedirs(os.path.dirname(path), exist_ok=True)
    with open(path, 'w', encoding='utf-8') as f:
        json.dump(obj, f, indent=2, ensure_ascii=False)


def get_raw_texts():
    """Returns a list of raw story-text blobs to parse, from whichever
    source triggered this run. Empty list if this is a rebucket-only
    scheduled run or there's nothing usable."""
    if os.environ.get("REBUCKET_ONLY") == "1":
        print("Debug: REBUCKET_ONLY mode - no new texts to process")
        return []

    p_str = os.environ.get("DISPATCH_CLIENT_PAYLOAD")
    if p_str and p_str.strip() and p_str.strip() != 'null':
        print(f"Debug: Found DISPATCH_CLIENT_PAYLOAD (length: {len(p_str)})")
        try:
            p = json.loads(p_str)
        except Exception as e:
            print(f"Warning: Could not parse DISPATCH_CLIENT_PAYLOAD: {e}")
            p = None
        if isinstance(p, dict):
            if p.get("raw_text"):
                print("Debug: Found raw_text in payload")
                return [p["raw_text"]]
            if p.get("raw_text_list"):
                print(f"Debug: Found raw_text_list with {len(p['raw_text_list'])} items")
                return list(p["raw_text_list"])
    else:
        print("Debug: No DISPATCH_CLIENT_PAYLOAD found")

    manual = os.environ.get("MANUAL_RAW_TEXT")
    if manual and manual.strip():
        print(f"Debug: Found MANUAL_RAW_TEXT (length: {len(manual)})")
        return [manual]

    print("Debug: No raw text found in any source")
    return []


def get_events():
    """Parses every raw text blob for this run. Returns (events,
    had_errors) — events is every story that parsed successfully
    across all blobs; had_errors is True if ANY story failed, so
    main() knows to exit non-zero even though good data still saved."""
    events = []
    had_errors = False
    raw_texts = get_raw_texts()
    
    if not raw_texts:
        print("No new stories to process")
        return events, had_errors
    
    for i, text in enumerate(raw_texts):
        print(f"Processing story {i+1}/{len(raw_texts)} (length: {len(text)} chars)")
        try:
            parsed, errors = parse_stories(text)
            events.extend(parsed)
            print(f"  Successfully parsed {len(parsed)} stories")
            for snippet, e in errors:
                had_errors = True
                print(f"::warning::Failed to parse a story (starts: \"{snippet}\"): {e}", file=sys.stderr)
        except Exception as e:
            had_errors = True
            print(f"::error::Failed to process story {i+1}: {e}", file=sys.stderr)
            
    print(f"Total events parsed: {len(events)}")
    return events, had_errors


def bucket_for_age(age: timedelta):
    """Returns None if item belongs in active_week (age <= 7 days)."""
    if age <= timedelta(days=7):
        return None
    if age <= timedelta(days=14):
        return "past_2_weeks"
    if age <= timedelta(days=30):
        return "past_1_month"
    if age <= timedelta(days=90):
        return "past_3_months"
    if age <= timedelta(days=180):
        return "past_6_months"
    if age <= timedelta(days=365):
        return "past_1_year"
    return "historical"


def main():
    print("=== Starting General Stock Report Logger ===")
    print(f"Time: {datetime.now(timezone.utc).isoformat()}")
    print(f"Working directory: {os.getcwd()}")
    
    new_events, had_errors = get_events()
    now = datetime.now(timezone.utc)

    active_items = load_data(ACTIVE_FILE, list)
    archive_dict = load_data(ARCHIVE_FILE, dict)
    if not isinstance(archive_dict, dict):
        archive_dict = {}
    for b in BUCKETS:
        if b not in archive_dict:
            archive_dict[b] = []

    print(f"Loaded {len(active_items)} active items and {sum(len(v) for v in archive_dict.values())} archived items")

    pool = list(new_events)
    pool.extend(active_items)
    for b in BUCKETS:
        pool.extend(archive_dict[b])

    # Dedupe on timestamp + header (one entry = one trading day's report)
    seen = set()
    unique_all = []
    for item in pool:
        uid = f"{item.get('header')}_{item.get('timestamp')}"
        if uid not in seen:
            seen.add(uid)
            unique_all.append(item)

    print(f"Total unique items after deduplication: {len(unique_all)}")

    new_active = []
    new_archive = {b: [] for b in BUCKETS}

    for item in unique_all:
        try:
            dt = datetime.fromisoformat(item['timestamp'].replace('Z', '+00:00'))
        except Exception:
            dt = now
        age = now - dt
        b = bucket_for_age(age)
        if b is None:
            new_active.append(item)
        else:
            new_archive[b].append(item)

    print(f"Items in active bucket: {len(new_active)}")
    for b in BUCKETS:
        print(f"Items in {b}: {len(new_archive[b])}")

    t_sort = lambda x: x.get('timestamp', '')
    new_active.sort(key=t_sort, reverse=True)
    for b in BUCKETS:
        new_archive[b].sort(key=t_sort, reverse=True)

    save_data(ACTIVE_FILE, new_active)
    save_data(ARCHIVE_FILE, new_archive)
    print("Data saved successfully")

    if had_errors:
        print("Had errors during parsing - exiting with code 1")
        sys.exit(1)
    
    print("=== Completed successfully ===")


if __name__ == "__main__":
    main()
