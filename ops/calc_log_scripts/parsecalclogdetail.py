import os
import re
import sqlite3
import sys
from datetime import datetime

import pandas as pd


# ── Patterns ──────────────────────────────────────────────────────────────────

# Matches record-count lines that appear between Starting / Finished.
RECORD_RE = re.compile(
    r'Updated:\s*(\d+)'                       # Script SQL:  "Updated: 13"
    r'|Affected Rows\s*:\s*(\d+)'             # Delete:      "Affected Rows : 3101"
    r'|Inserted\s+(\d+)\s+records'            # Insert:      "Inserted 3101 records."
    r'|Total rows updated:\s*(\d+)'           # Update:      "Total rows updated: 3101"
    r'|Updated\s+(\d+)\s+rows'               # Rollup:      "Updated 0 rows."
    r'|Number of eligible credits:\s*(\d+)'   # Crediting:   "Number of eligible credits: 1784"
)

# Noise lines to exclude from captured query text.
SKIP_BODY_RE = re.compile(
    r'^Processing .+ for .+\('                # "Processing Sales Rollup for Name (id)"
    r'|^Running Plan Group'                   # "Running Plan Group 1 ---..."
    r'|^-{10,}'                               # separator lines
    r'|^.+: Clearing approval results for participant\.'
)

# Starting / Finished event lines (two formats).
EVENT_RE = re.compile(
    r'(\d{4}-\d{2}-\d{2} \d{2}:\d{2}:\d{2}\.\d{1,3})'
    r' - (Starting|Finished)'
    r' (?:"(.+?)" - (.+)|(Plan Calculation|Crediting Component) (.+))'
)

TRACE_ID_RE = re.compile(r'Process trace ID:\s*(\w+)', re.IGNORECASE)


# ── Helpers ───────────────────────────────────────────────────────────────────

def read_log(path: str):
    """Yield lines from a log file with a large read buffer."""
    with open(path, buffering=8 * 1024 * 1024) as f:
        yield from f


def parse_event(line: str):
    """
    Return (timestamp, event_type, transform, step) for Starting/Finished lines,
    or None for all other lines.
    """
    if 'Starting' not in line and 'Finished' not in line:
        return None
    m = EVENT_RE.search(line)
    if not m:
        return None
    ts, event = m.group(1), m.group(2)
    # Quoted format: "transform" - step
    if m.group(3):
        return ts, event, m.group(3), m.group(4)
    # Unquoted format: Plan Calculation / Crediting Component <step>
    return ts, event, m.group(5), m.group(6)


def parse_body(lines: list):
    """
    Extract record counts and query text from body lines between events.

    Returns:
        records  – total row count (int) or None if no count lines found
        query    – non-count text joined as a string, or None if empty
    """
    total = 0
    found_records = False
    query_parts = []

    for line in lines:
        stripped = line.strip()
        m = RECORD_RE.search(stripped)
        if m:
            found_records = True
            total += int(next(g for g in m.groups() if g is not None))
        elif stripped and not SKIP_BODY_RE.match(stripped):
            query_parts.append(stripped)

    # Drop trailing blank entries
    while query_parts and not query_parts[-1]:
        query_parts.pop()

    records = total if found_records else None
    query = '\n'.join(query_parts) if query_parts else None
    return records, query


def log_filename(path: str) -> str:
    """Return just the filename portion of a path."""
    return path.rsplit('/', 1)[-1]


# ── Core parser ───────────────────────────────────────────────────────────────

def parse_log(path: str) -> list[dict]:
    """
    Parse a log file and return a list of completed step records.

    Each Starting event is paired with its immediately following Finished event.
    Body lines between them are parsed for record counts and query text.
    """
    print(f"Parsing {path}")

    filename = log_filename(path)
    completed = []
    current = None   # fields of the open Starting event
    body = []
    trace_id = None

    for raw_line in read_log(path):
        line = raw_line.rstrip('\n')

        if trace_id is None:
            m = TRACE_ID_RE.match(line)
            if m:
                trace_id = m.group(1)

        event = parse_event(line)

        if event is None:
            if current is not None:
                body.append(line)
            continue

        ts, event_type, transform, step = event

        if event_type == 'Starting':
            current = dict(
                fn=filename,
                step=step,
                transform=transform,
                start_time=datetime.strptime(ts, '%Y-%m-%d %H:%M:%S.%f'),
                trace_id=trace_id,
            )
            body = []

        elif event_type == 'Finished' and current and transform == current['transform']:
            records, query = parse_body(body)
            end_time = datetime.strptime(ts, '%Y-%m-%d %H:%M:%S.%f')
            duration = (end_time - current['start_time']).total_seconds()
            completed.append({
                'fn':                  current['fn'],
                'step':                current['step'],
                'transform':           current['transform'],
                'start_time':          current['start_time'],
                'end_time':            end_time,
                'duration_in_seconds': duration,
                'records_updated':     records,
                'query':               query,
                'trace_id':            current['trace_id'],
            })
            current = None
            body = []

    return completed


# ── Output ────────────────────────────────────────────────────────────────────

def open_db(db_path: str) -> sqlite3.Connection:
    con = sqlite3.connect(db_path)
    con.execute("""
        CREATE TABLE IF NOT EXISTS queries (
            id         INTEGER PRIMARY KEY AUTOINCREMENT,
            log_file   TEXT,
            step_index INTEGER,
            query_text TEXT
        )
    """)
    con.commit()
    return con


def save_queries_to_db(con: sqlite3.Connection, log_file: str, steps: list[dict]):
    """
    Insert query text for each step into the DB.
    Replaces the 'query' value in-place with the integer row ID.
    """
    for idx, step in enumerate(steps):
        if not step.get('query'):
            continue
        cur = con.execute(
            "INSERT INTO queries (log_file, step_index, query_text) VALUES (?, ?, ?)",
            (log_file, idx + 1, step['query']),
        )
        step['query'] = cur.lastrowid
    con.commit()


def main(output: str, *log_files: str):
    output_abs = os.path.abspath(output)
    output_dir = os.path.dirname(output_abs)
    output_base = os.path.splitext(os.path.basename(output_abs))[0]

    db_path = os.path.join(output_dir, output_base + '.db')
    con = open_db(db_path)

    mode = 'a' if os.path.exists(output) else 'w'
    writer = pd.ExcelWriter(output, engine='openpyxl', mode=mode)

    for path in log_files:
        steps = parse_log(path)
        sheet_name = log_filename(path)
        save_queries_to_db(con, sheet_name, steps)
        pd.DataFrame(steps).to_excel(writer, sheet_name=sheet_name, index=False)

    writer.close()
    con.close()
    print(f"Queries saved to: {db_path}")


if __name__ == '__main__':
    main(sys.argv[1], *sys.argv[2:])

