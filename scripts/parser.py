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
#
# Format tolerance (Oct 2026 update):
#   - Accepts both Markdown '##' headings and emoji-prefixed plain
#     headings (📊 / 📈 / 📉 / 💡 / 🗒 …).
#   - Accepts both '*' and '•' (Unicode bullet) as list markers.
#   - Parses both Markdown '|' tables and tab-separated (TSV) tables.
#   - Section boundaries stop at the next major heading even when the
#     report has no '-----' horizontal rules.

import re
from datetime import datetime, timezone

MONTHS = {
    "january": 1, "february": 2, "march": 3, "april": 4, "may": 5, "june": 6,
    "july": 7, "august": 8, "september": 9, "october": 10, "november": 11, "december": 12,
}

# Match either '*' or the Unicode bullet '•', optionally preceded by
# whitespace/tabs.
BULLET_RE = re.compile(r'^[\t ]*[•*]\s*(.*)$')

# Shared “next major section” boundaries so sections do not bleed when
# the report has no ----- dividers.
# Headings are required to look like real section titles (emoji or ##,
# or a short line that starts with Top/Major/Catalyst), so we do not
# match table column headers such as "Primary Catalyst & …".
_HEADING = (
    r'(?:'
    r'#{1,3}\s*'                                    # markdown ##
    r'|[📊📈📉💡🗒📁]\s*'                            # emoji prefix
    r')'
)
_END_AT_NEXT_MAJOR = (
    r'\n' + _HEADING + r'?Major Market (?:Indexes|Indices)[^\n]*\n'
    r'|\n' + _HEADING + r'?(?:Top\s+)?(?:\d+\s+)?Gainers?[^\n]*\n'
    r'|\n' + _HEADING + r'?(?:Top\s+)?(?:\d+\s+)?Losers?[^\n]*\n'
    # Catalyst end-boundary requires a heading marker so the table
    # column "Primary Catalyst & …" is not treated as a section break.
    r'|\n' + _HEADING + r'Catalyst[^\n]*\n'
    r'|\n-{5,}\n'
    r'|\Z'
)


def _strip_refs(text):
    """Remove markdown links (keep link text), footnote brackets like
    [1, 2, 3] or [[1.2.1](url)], long bare [citation text] blocks,
    and collapse extra whitespace."""
    if not text:
        return ""
    t = text
    # [label](url) -> label
    t = re.sub(r'\[([^\]]+)\]\(([^)]+)\)', r'\1', t)
    # leftover bracket groups that are just numbers/refs, e.g. [1, 2, 3] or [1.1.4]
    t = re.sub(r'\[[\d,\.\s]+\]', '', t)
    # long bare [citation / source title] blocks the agent leaves behind
    t = re.sub(r'\[[^\]]{8,}\]', '', t)
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
    # Also matches bare "Tuesday, October 13, 2026's Report" lines
    # (with or without a leading emoji on the same line).
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


def _section(text, start_patterns, end_pattern=None):
    """Grab the text between the first line matching any of
    start_patterns and the next major section (or horizontal rule / EOF)."""
    if end_pattern is None:
        end_pattern = _END_AT_NEXT_MAJOR
    for sp in start_patterns:
        m = re.search(sp, text, re.IGNORECASE)
        if m:
            rest = text[m.end():]
            end_m = re.search(end_pattern, rest, re.IGNORECASE)
            return rest[:end_m.start()] if end_m else rest
    return ""


def _is_bullet(line):
    return bool(BULLET_RE.match(line))


def _bullet_content(line):
    m = BULLET_RE.match(line)
    return m.group(1).strip() if m else ""


def _parse_indexes(section_text):
    """Parse index lines of the form:
        • S&P 500 Index (.INX): 7,805.45 (-1.79 pts / -0.02%)
        * S&P 500: 4500.12 (+12.3 pts / +0.27%)
    """
    indexes = []
    # Prefer structured bullet lines
    bullet_pat = re.compile(
        r'^\s*[•*]\s+([^:\n]+?):\s+(\$?[\d,]+\.?\d*)\s*\(([+-][\d,\.]+\s*pts)\s*/\s*([+-][\d.]+%)\)',
        re.MULTILINE
    )
    for m in bullet_pat.finditer(section_text):
        name, value, change, pct = m.groups()
        indexes.append({
            "name": name.strip(),
            "value": value.strip(),
            "change": change.strip(),
            "pct": pct.strip(),
        })
    if indexes:
        return indexes

    # Fallback: original * -only pattern (no bullet required)
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


def _split_table_line(line):
    """Split a table row into cells. Supports Markdown '|' tables and
    tab-separated (or multi-space) TSV-style tables."""
    line = line.strip()
    if not line:
        return []
    # Markdown pipe table
    if line.startswith("|") or "|" in line:
        return [c.strip() for c in line.strip().strip("|").split("|")]
    # Tab-separated
    if "\t" in line:
        return [c.strip() for c in line.split("\t")]
    # Fallback: 2+ spaces as delimiter (fragile, but better than nothing)
    return [c.strip() for c in re.split(r'\s{2,}', line) if c.strip()]


def _parse_table(section_text):
    """Parse a table by reading the header row and matching columns by
    keyword, not by position or column count. Supports both Markdown
    '|' tables and tab-separated tables used in the emoji-style reports.
    """
    raw_lines = [l for l in section_text.split("\n") if l.strip()]
    if not raw_lines:
        return []

    # Locate header + optional separator + data rows.
    # For Markdown: look for a |---| separator.
    # For TSV: the first line that contains "Ticker" (or similar) is the header.
    header_idx = None
    sep_idx = None
    for i, line in enumerate(raw_lines):
        stripped = line.strip()
        # Markdown separator
        if re.match(r'^\|[\s\-:|]+\|\s*$', stripped):
            sep_idx = i
            if i > 0:
                header_idx = i - 1
            break
        # TSV / plain header: line that looks like a header row
        lower = stripped.lower()
        if header_idx is None and (
            ("ticker" in lower and ("company" in lower or "price" in lower or "change" in lower))
            or (lower.startswith("ticker") and "\t" in stripped)
        ):
            header_idx = i
            # No separator in TSV; data starts on the next line
            break

    if header_idx is None:
        return []

    header_cells = _split_table_line(raw_lines[header_idx])
    if not header_cells:
        return []
    header_lower = [h.lower() for h in header_cells]

    # Data starts after separator (Markdown) or immediately after header (TSV)
    if sep_idx is not None:
        data_lines = raw_lines[sep_idx + 1:]
    else:
        data_lines = raw_lines[header_idx + 1:]

    idx_ticker = _find_col(header_lower, lambda h: "ticker" in h)
    idx_company = _find_col(header_lower, lambda h: "company" in h)
    # If "ticker" and "company" both land on the SAME header cell (e.g.
    # "Ticker / Company"), it's one combined column, not two.
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
    idx_catalyst = _find_col(header_lower, lambda h: "catalyst" in h or "reason" in h or "focus" in h)

    if idx_ticker is None:
        return []

    def cell(cells, idx):
        return cells[idx].strip() if idx is not None and idx < len(cells) else ""

    rows = []
    for line in data_lines:
        # Stop if we hit a new section-looking line
        if re.match(r'^(?:#{1,3}\s*)?(?:📊|📈|📉|💡)', line.strip()):
            break
        cells = _split_table_line(line)
        if len(cells) < 2:
            continue

        ticker_cell = cell(cells, idx_ticker)
        if not ticker_cell:
            continue
        # Skip accidental re-parsing of a header-like row
        if ticker_cell.lower() in ("ticker", "symbol"):
            continue

        if idx_company is not None and not combined_ticker_company:
            ticker = ticker_cell
            company = cell(cells, idx_company)
        else:
            # Combined cell, e.g. "NASDAQ: MRNA (Moderna)" or a bare "SNOW"
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
        if not _is_bullet(line):
            # also accept plain "* " that might have been missed
            stripped = line.strip()
            if stripped.startswith("*"):
                content = stripped.lstrip("*").strip()
            else:
                continue
        else:
            content = _bullet_content(line)
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
    if any(w in hl for w in ["pull back", "pullback", "slip", "fall", "drop", "decline", "lower", "flatline", "muted"]):
        return "DOWN"
    return "MIXED"


def _headline(text):
    """First substantial prose paragraph before the first section
    boundary — skips the date/agent-name/header metadata lines."""
    body = text
    # Cut off anything from the first major section or "------" divider
    cut = re.search(
        r'\n(?:#{1,3}\s*)?(?:📊|📈|📉|💡)?\s*(?:Major Market (?:Indexes|Indices)|(?:Top\s+)?(?:\d+\s+)?Gainers?|(?:Top\s+)?(?:\d+\s+)?Losers?|Catalyst)',
        body, re.IGNORECASE
    )
    if cut:
        body = body[:cut.start()]
    else:
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
        # Skip emoji + date header lines
        if re.search(r"\d{4}'s Report$", stripped):
            continue
        if re.match(r'^[🗒📁📊📈📉💡].{0,80}$', stripped) and len(stripped) < 90:
            continue
        if len(stripped) > 40:
            return _strip_refs(stripped)
    # Fallback
    for line in lines:
        stripped = line.strip()
        if len(stripped) > 20 and not re.search(r"\d{4}'s Report$", stripped):
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
        _HEADING + r'?Major Market (?:Indexes|Indices)[^\n]*\n',
        _HEADING + r'?Major U\.?S\.? stock indexes[^\n]*\n',
    ])
    indexes = _parse_indexes(idx_section)

    # Match on the stable keyword only ("Gainers"/"Losers"), not the
    # exact phrase — headings have varied between "Top Gainers",
    # "Top 10 Gainers", "Top Gainers (NYSE & NASDAQ)", etc.
    # Require a real heading marker so we don't match table body text.
    gainers_section = _section(text, [
        _HEADING + r'?(?:Top\s+)?(?:\d+\s+)?Gainers?[^\n]*\n',
    ])
    gainers = _parse_table(gainers_section)

    losers_section = _section(text, [
        _HEADING + r'?(?:Top\s+)?(?:\d+\s+)?Losers?[^\n]*\n',
    ])
    losers = _parse_table(losers_section)

    # Same idea: "Catalyst" alone catches both "Core Underlying
    # Catalysts" and "Catalyst & Earnings Focus". Require a real
    # heading marker (emoji or ##) so the table column
    # "Primary Catalyst & …" is never matched as a section start.
    catalysts_section = _section(text, [
        _HEADING + r'Catalyst[^\n]*\n',
        r'(?m)^Catalyst(?:s|&|\s)[^\n]{0,40}\n',  # bare short heading fallback
    ])
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
