# scripts/parser.py — General Stock Report
#
# Converts ONE raw narrative report (the text your Google agent writes,
# same shape as generalstockreport.txt) into the structured dict
# logger.py needs. This is best-effort extraction, not a strict
# validator: if a field genuinely isn't present in the source text
# (e.g. the filtered $7-floor report drops the Sector/Industry column
# entirely), that field comes back empty rather than invented.
#
# Known limitation, stated plainly rather than hidden: if the agent's
# wording drifts from the patterns below (different heading emoji,
# different table column order), extraction for that section will come
# back partial or empty. This parser was built against the 3 sample
# reports you provided — it is not guaranteed against formats it has
# never seen.

import re
from datetime import datetime, timezone

MONTHS = {
    "january": 1, "february": 2, "march": 3, "april": 4, "may": 5, "june": 6,
    "july": 7, "august": 8, "september": 9, "october": 10, "november": 11, "december": 12,
}


def _strip_refs(text):
    """Remove markdown links (keep link text), footnote brackets like
    [1, 2, 3] or [[1.2.1](url)], and collapse extra whitespace."""
    if not text:
        return ""
    t = text
    # [label](url) -> label
    t = re.sub(r'\[([^\]]+)\]\(([^)]+)\)', r'\1', t)
    # leftover bracket groups that are just numbers/refs, e.g. [1, 2, 3] or [1.1.4]
    t = re.sub(r'\[[\d,\.\s]+\]', '', t)
    # leftover empty double-bracket remnants like [[]]
    t = re.sub(r'\[\s*\]', '', t)
    t = re.sub(r'\s{2,}', ' ', t).strip()
    if t and not t.endswith((".", "!", "?", '"')):
        t += "."
    return t


def _split_stories(raw_text):
    """Defensively re-split on a line containing only '===', in case a
    single raw_text field contains multiple stories."""
    normalized = raw_text.replace("\r\n", "\n")
    parts = re.split(r'\n===\n|^===\n|\n===$', normalized)
    return [p.strip() for p in parts if p.strip()]


def _find_header_and_date(text):
    """Returns (header_string, (year, month, day)) using whichever of
    three patterns is present, in order of preference."""
    # Pattern A: "## Friday, August 28, 2026's Report"
    m = re.search(r"##\s*((\w+),\s*(\w+)\s+(\d{1,2}),\s*(\d{4})'s Report)", text)
    if m:
        header, _, month_name, day, year = m.groups()
        month = MONTHS.get(month_name.lower())
        if month:
            return header, (int(year), month, int(day))

    # Pattern B: "... report for Monday, August 24, 2026" (prose fallback)
    m = re.search(r"(\w+),\s*(\w+)\s+(\d{1,2}),\s*(\d{4})", text)
    if m:
        weekday, month_name, day, year = m.groups()
        month = MONTHS.get(month_name.lower())
        if month:
            header = f"{weekday}, {month_name} {day}, {year}'s Report"
            return header, (int(year), month, int(day))

    # Pattern C: just "August 28, 4:00 PM" at the top — no weekday given.
    m = re.search(r"^(\w+)\s+(\d{1,2}),\s*\d{1,2}:\d{2}\s*[AP]M", text, re.MULTILINE)
    if m:
        month_name, day = m.groups()
        month = MONTHS.get(month_name.lower())
        if month:
            now = datetime.now(timezone.utc)
            year = now.year
            try:
                weekday = datetime(year, month, int(day)).strftime("%A")
            except ValueError:
                weekday = "Unknown"
            header = f"{weekday}, {month_name} {day}, {year}'s Report"
            return header, (year, month, int(day))

    return None, None


def _section(text, start_patterns, end_pattern=r'\n-{5,}\n|\Z'):
    """Grab the text between the first line matching any of
    start_patterns and the next horizontal rule (or end of text)."""
    for sp in start_patterns:
        m = re.search(sp, text)
        if m:
            rest = text[m.end():]
            end_m = re.search(end_pattern, rest)
            return rest[:end_m.start()] if end_m else rest
    return ""


def _parse_indexes(section_text):
    indexes = []
    pattern = re.compile(
        r'^\*\s+([^:*\n][^:\n]*?):\s+(\$?[\d,]+\.?\d*)\s*\(([+-][\d,\.]+\s*pts)\s*/\s*([+-][\d.]+%)\)',
        re.MULTILINE
    )
    for m in pattern.finditer(section_text):
        name, value, change, pct = m.groups()
        indexes.append({
            "name": name.strip(),
            "value": value.strip(),
            "change": change.strip(),
            "pct": pct.strip(),
        })
    return indexes


def _parse_table(section_text):
    """Parse a markdown table, tolerant of a missing Sector/Industry
    column (the $7-floor filtered report drops it)."""
    lines = [l for l in section_text.split("\n") if l.strip().startswith("|")]
    # Drop the separator row (---|---|---) and the header row.
    data_lines = [l for l in lines if not re.match(r'^\|[\s\-:|]+\|\s*$', l)]
    if len(data_lines) < 2:
        return []
    data_lines = data_lines[1:]  # first remaining row is the header labels

    rows = []
    for line in data_lines:
        cells = [c.strip() for c in line.strip().strip("|").split("|")]
        if len(cells) == 6:
            ticker_cell, sector, price, change, pct, catalyst = cells
        elif len(cells) == 5:
            ticker_cell, price, change, pct, catalyst = cells
            sector = ""
        else:
            continue

        tm = re.match(r'^(.*?)\s*\(([^)]+)\)\s*$', ticker_cell)
        if tm:
            ticker, company = tm.groups()
        else:
            ticker, company = ticker_cell, ""

        rows.append({
            "ticker": ticker.strip(),
            "company": company.strip(),
            "sector": sector.strip(),
            "price": price.strip(),
            "change": change.strip(),
            "pct": pct.strip(),
            "catalyst": _strip_refs(catalyst),
        })
    return rows


def _parse_catalysts(section_text):
    catalysts = []
    for line in section_text.split("\n"):
        line = line.strip()
        if line.startswith("*"):
            content = line.lstrip("*").strip()
            if len(content) > 3:
                catalysts.append(_strip_refs(content))
    return catalysts


def _market_direction(indexes, headline):
    up = sum(1 for ix in indexes if ix["pct"].startswith("+"))
    down = sum(1 for ix in indexes if ix["pct"].startswith("-"))
    if up > down:
        return "UP"
    if down > up:
        return "DOWN"
    if up == down and up > 0:
        return "MIXED"
    hl = (headline or "").lower()
    if any(w in hl for w in ["rally", "surge", "soar", "gain", "jump", "climb"]):
        return "UP"
    if any(w in hl for w in ["pull back", "pullback", "slip", "fall", "drop", "decline", "lower"]):
        return "DOWN"
    return "MIXED"


def _headline(text):
    """First substantial prose paragraph before the first section
    boundary — skips the date/agent-name/header metadata lines."""
    body = text
    # Cut off anything from the first "------" divider onward — headline
    # only ever lives before it.
    div = re.search(r'\n-{5,}\n', body)
    if div:
        body = body[:div.start()]
    lines = [l for l in body.split("\n") if l.strip()]
    for line in lines:
        stripped = line.strip()
        if re.match(r'^\w+\s+\d{1,2},\s*\d{1,2}:\d{2}\s*[AP]M$', stripped):
            continue  # date/time line
        if stripped.lower() in ("stock market update agent",):
            continue  # agent name line
        if stripped.startswith("##"):
            continue  # the "'s Report" header itself
        if len(stripped) > 20:
            return _strip_refs(stripped)
    return ""


def parse_story(text):
    """Parse one '===' - delimited story block into the schema
    logger.py expects. Raises ValueError if no date/header can be
    found at all (that's the one thing we can't proceed without)."""
    header, ymd = _find_header_and_date(text)
    if not header or not ymd:
        raise ValueError("could not find a date or 'Report' header anywhere in this story")

    year, month, day = ymd
    timestamp = f"{year:04d}-{month:02d}-{day:02d}T20:00:00+00:00"

    headline = _headline(text)

    idx_section = _section(text, [
        r'##[^\n]*Major Market (?:Indexes|Indices)[^\n]*\n',
    ])
    indexes = _parse_indexes(idx_section)

    gainers_section = _section(text, [r'##[^\n]*Top Gainers[^\n]*\n'])
    gainers = _parse_table(gainers_section)

    losers_section = _section(text, [r'##[^\n]*Top Losers[^\n]*\n'])
    losers = _parse_table(losers_section)

    catalysts_section = _section(text, [r'##[^\n]*Underlying Catalysts[^\n]*\n'])
    catalysts = _parse_catalysts(catalysts_section)

    return {
        "timestamp": timestamp,
        "header": header,
        "headline": headline,
        "market_direction": _market_direction(indexes, headline),
        "indexes": indexes,
        "gainers": gainers,
        "losers": losers,
        "catalysts": catalysts,
    }


def parse_stories(raw_text):
    """Split raw_text on '===' defensively and parse each block.
    Returns (events, errors) — errors is a list of (snippet, exception)
    for blocks that couldn't be parsed, so callers can log them without
    losing the blocks that DID parse."""
    events, errors = [], []
    for block in _split_stories(raw_text):
        try:
            events.append(parse_story(block))
        except Exception as e:
            snippet = block.strip().split("\n")[0][:80]
            errors.append((snippet, e))
    return events, errors
