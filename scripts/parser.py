# scripts/parser.py — General Stock Report
#
# Converts ONE raw narrative report (the text your Google agent writes,
# same shape as generalstockreport.txt) into the structured dict
# logger.py needs. This is best-effort extraction, not a strict
# validator: if a field genuinely isn't present in the source text
# (e.g. the filtered $7-floor report drops the Sector/Industry column
# entirely), that field comes back empty rather than invented.
#
# ROBUSTNESS NOTE (read this before "fixing" a new format drift):
# the agent's headings and table columns have already drifted at least
# three times in the sample data this parser was built against:
#   - table columns: "TICKER (Company)" combined cell -> "Ticker" /
#     "Company Name" as two separate cells
#   - gainers/losers heading: "Top Gainers" -> "Top 10 Gainers"
#   - catalysts heading: "Core Underlying Catalysts" -> "Catalyst &
#     Earnings Focus"
# To survive the *next* drift without another emergency fix, table
# parsing is header-driven (matches columns by keyword, not position
# or count) and section boundaries match on the one stable keyword
# ("Gainers", "Losers", "Catalyst") rather than the exact phrase around
# it. If a future report renames a column to something with none of
# the keywords below, that field will come back empty rather than
# invented — check the keyword lists in _parse_table first.

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
    # Pattern A: "## [emoji ]Friday, August 28, 2026's Report"
    # Non-greedy [^\n]*? between ## and the weekday tolerates an emoji
    # or any other decoration the agent puts right after the ##
    # (seen: "## 🗒 Thursday, September 10, 2026's Report").
    m = re.search(r"##[^\n]*?(\w+,\s*(\w+)\s+(\d{1,2}),\s*(\d{4})'s Report)", text)
    if m:
        header, month_name, day, year = m.groups()
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


def _find_col(header_cells_lower, *predicates):
    """Returns the index of the first header cell matching ANY of the
    given predicates (each predicate is a callable taking the lowercased
    cell text), or None if no cell matches."""
    for i, h in enumerate(header_cells_lower):
        for pred in predicates:
            if pred(h):
                return i
    return None


def _parse_table(section_text):
    """Parse a markdown table by reading the header row and matching
    columns by keyword, not by position or column count. This survives
    the agent reordering columns, adding/dropping Sector/Industry, or
    splitting a combined "Ticker (Company)" cell into separate Ticker
    and Company columns — all of which have already happened across
    the sample reports this parser was built against."""
    lines = [l for l in section_text.split("\n") if l.strip().startswith("|")]
    if not lines:
        return []

    sep_idx = next(
        (i for i, l in enumerate(lines) if re.match(r'^\|[\s\-:|]+\|\s*$', l)),
        None
    )
    # Need a header row before the separator, and at least one data row after.
    if sep_idx is None or sep_idx == 0 or sep_idx == len(lines) - 1:
        return []

    header_cells = [c.strip() for c in lines[0].strip().strip("|").split("|")]
    header_lower = [h.lower() for h in header_cells]
    data_lines = lines[sep_idx + 1:]

    idx_ticker = _find_col(header_lower, lambda h: "ticker" in h)
    idx_company = _find_col(header_lower, lambda h: "company" in h)
    # If "ticker" and "company" both land on the SAME header cell (e.g.
    # "Ticker / Company"), it's one combined column, not two — the
    # combined-cell regex below handles splitting it.
    combined_ticker_company = (
        idx_ticker is not None and idx_ticker == idx_company
    )
    idx_sector = _find_col(header_lower, lambda h: "sector" in h or "industry" in h)
    idx_price = _find_col(
        header_lower,
        lambda h: ("price" in h or "clos" in h) and "change" not in h
    )
    idx_change = _find_col(
        header_lower,
        lambda h: "change" in h and "%" not in h and "percent" not in h
    )
    idx_pct = _find_col(header_lower, lambda h: "%" in h or "percent" in h)
    idx_catalyst = _find_col(header_lower, lambda h: "catalyst" in h or "reason" in h)

    if idx_ticker is None:
        # Can't identify which column is even the ticker — nothing
        # reliable to extract from this table.
        return []

    def cell(cells, idx):
        return cells[idx].strip() if idx is not None and idx < len(cells) else ""

    rows = []
    for line in data_lines:
        cells = [c.strip() for c in line.strip().strip("|").split("|")]
        if len(cells) < 2:
            continue

        ticker_cell = cell(cells, idx_ticker)
        if not ticker_cell:
            continue

        if idx_company is not None and not combined_ticker_company:
            ticker = ticker_cell
            company = cell(cells, idx_company)
        else:
            # Combined cell, e.g. "NASDAQ: MRNA (Moderna)" or a bare
            # "SNOW" with no company at all.
            tm = re.match(r'^(.*?)\s*\(([^)]+)\)\s*$', ticker_cell)
            if tm:
                ticker, company = tm.groups()
            else:
                ticker, company = ticker_cell, ""

        rows.append({
            "ticker": ticker.strip(),
            "company": company.strip(),
            "sector": cell(cells, idx_sector),
            "price": cell(cells, idx_price),
            "change": cell(cells, idx_change),
            "pct": cell(cells, idx_pct),
            "catalyst": _strip_refs(cell(cells, idx_catalyst)),
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

    # Match on the stable keyword only ("Gainers"/"Losers"), not the
    # exact phrase — headings have varied between "Top Gainers",
    # "Top 10 Gainers", "Top Gainers (NYSE & NASDAQ)", etc.
    gainers_section = _section(text, [r'##[^\n]*Gainers[^\n]*\n'])
    gainers = _parse_table(gainers_section)

    losers_section = _section(text, [r'##[^\n]*Losers[^\n]*\n'])
    losers = _parse_table(losers_section)

    # Same idea: "Catalyst" alone catches both "Core Underlying
    # Catalysts" and "Catalyst & Earnings Focus".
    catalysts_section = _section(text, [r'##[^\n]*Catalyst[^\n]*\n'])
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
