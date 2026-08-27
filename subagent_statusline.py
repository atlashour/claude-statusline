#!/usr/bin/env python3
"""Subagent status line — one row per visible subagent in the agent panel.

Replaces the default `name · description · token count` row with a row that
answers the question you actually have while agents are running: how close is
each one to its context limit, and on what model and effort is it running.

Row layout, left to right, each part dropped from the right when the row
does not fit the `columns` budget the harness declares:

    ▸ explore:parser  opus-5 high  ████░░ 24%  48k  locate the tokenizer…
    │ │               │            │           │    └ description
    │ │               │            │           └ tokens consumed
    │ │               │            └ context gauge, coloured by saturation
    │ │               └ resolved model, and effort when explicitly set
    │ └ label, falling back to the task name
    └ status glyph

Design notes:

1. The context gauge only appears when `contextWindowSize` is present. The
   harness omits that field (with `model`) until the task's model resolves,
   and both require Claude Code v2.1.205 or later; a percentage computed
   against a guessed window would be a confident lie, so there isn't one.

2. `effort` is absent when the subagent inherits the session's effort level,
   and is either a level string or a numeric token budget. Both render; the
   absent case renders nothing rather than repeating the session's level,
   because the field's whole meaning is "this one differs". Requires
   v2.1.214 or later.

3. Width comes from the `columns` field in the payload, NOT from the COLUMNS
   environment variable: the harness declares the usable row width here, and
   it is narrower than the terminal because the panel indents its rows.

4. This module depends on statusline.py for width and formatting helpers so
   the two lines stay visually identical. The dependency points this way on
   purpose: the main status line is the critical one and stays self-contained.
   If the import fails, every helper degrades to a plain-ASCII equivalent and
   rows still render, uncoloured.

Input: one JSON object on stdin with the base hook fields, `columns`, and a
`tasks` array. Output: one JSON line per row, {"id": ..., "content": ...}.
Tasks without an id are skipped, which leaves their default rendering intact.

No third-party dependencies; Python 3.7+.
"""
import json
import os
import sys

sys.path.insert(0, os.path.dirname(os.path.abspath(__file__)))

try:
    from statusline import VERSION, A, elide, human, num, visible_len
except Exception:                                    # degrade, never disappear
    VERSION = "5.0.0"
    A = {k: "" for k in ("reset", "dim", "bold", "green",
                         "yellow", "red", "cyan", "gray")}

    def num(x, default=0.0):
        try:
            return float(x)
        except (TypeError, ValueError):
            return default

    def human(n):
        return f"{int(n) // 1000}k" if n >= 1000 else str(int(n))

    def visible_len(s):
        return len(s)

    def elide(s, budget):
        return s if len(s) <= budget else s[:max(0, budget - 1)] + "…"

# --- Tunables -------------------------------------------------------------
BAR_W      = 6    # gauge width in cells (narrower than the main line's 12)
WARN_PCT   = 60   # gauge turns yellow at this % of the context window
HOT_PCT    = 85   # ...and red here
NAME_MAX   = 22   # display cells for the task label
FALLBACK_COLS = 80
# ---------------------------------------------------------------------------

GLYPH = {"running": "▸", "pending": "·", "queued": "·",
         "completed": "✓", "done": "✓", "failed": "✗", "error": "✗"}


def short_model(model_id: str) -> str:
    """claude-opus-5 → opus-5; claude-haiku-4-5-20251001 → haiku-4-5.

    Trims the vendor prefix and any trailing date stamp, which carry no
    information a human reading a crowded panel needs.
    """
    m = str(model_id).strip()
    if not m:
        return ""
    m = m.split("/")[-1]                              # bedrock/vertex prefixes
    for prefix in ("anthropic.", "claude-", "us.anthropic.claude-"):
        if m.startswith(prefix):
            m = m[len(prefix):]
    parts = m.split("-")
    if len(parts) > 1 and parts[-1].isdigit() and len(parts[-1]) == 8:
        parts = parts[:-1]                            # drop a 20251001 stamp
    return "-".join(parts)


def gauge(tokens: float, window: float) -> str:
    """Coloured bar + percentage of the task's own context window."""
    pct = tokens / window * 100 if window else 0
    col = A["red"] if pct >= HOT_PCT else A["yellow"] if pct >= WARN_PCT else A["green"]
    fill = max(0, min(BAR_W, round(pct / 100 * BAR_W)))
    bar = "█" * fill + "░" * (BAR_W - fill)
    return f"{col}{bar} {round(pct)}%{A['reset']}"


def render_row(task: dict, columns: int) -> str:
    """The row body for one task, trimmed to fit `columns` display cells."""
    status = str(task.get("status") or "").lower()
    glyph = GLYPH.get(status, "·")
    label = str(task.get("label") or task.get("name") or "agent")
    head = f"{glyph} {A['bold']}{A['cyan']}{elide(label, NAME_MAX)}{A['reset']}"

    # Optional parts, in drop order (rightmost goes first).
    parts = []

    model = short_model(task.get("model") or "")
    effort = task.get("effort")
    meta = model
    if effort is not None and effort != "":
        shown = human(num(effort)) if isinstance(effort, (int, float)) else str(effort)
        meta = f"{meta} {shown}".strip()
    if meta:
        parts.append(f"{A['dim']}{meta}{A['reset']}")

    window = num(task.get("contextWindowSize"), 0)
    tokens = num(task.get("tokenCount"), 0)
    if window > 0:
        parts.append(gauge(tokens, window))
    if tokens > 0:
        parts.append(f"{A['dim']}{human(tokens)}{A['reset']}")

    desc = str(task.get("description") or "").strip()

    sep = "  "
    while True:
        line = sep.join([head] + parts)
        room = columns - visible_len(line) - len(sep)
        if desc and room > 4:
            line += sep + f"{A['dim']}{elide(desc, room)}{A['reset']}"
        if visible_len(line) <= columns or not parts:
            return line
        parts.pop()


def render_rows(d: dict) -> list:
    """One JSON line per task that carries an id. Never raises."""
    if not isinstance(d, dict):
        return []
    try:
        columns = int(num(d.get("columns"), FALLBACK_COLS)) or FALLBACK_COLS
    except Exception:
        columns = FALLBACK_COLS

    rows = []
    for task in (d.get("tasks") or []):
        if not isinstance(task, dict):
            continue
        tid = task.get("id")
        if not tid:                       # no id → keep the default rendering
            continue
        try:
            content = render_row(task, columns)
        except Exception:
            continue                      # one bad task drops its row only
        rows.append(json.dumps({"id": str(tid), "content": content},
                               ensure_ascii=False))
    return rows


def main() -> None:
    try:
        sys.stdout.reconfigure(encoding="utf-8", errors="replace")
    except Exception:
        pass
    if "--version" in sys.argv[1:]:
        sys.stdout.write(f"subagent-statusline {VERSION}\n")
        return
    try:
        d = json.load(sys.stdin)
    except Exception:
        d = {}
    try:
        rows = render_rows(d)
    except Exception:
        rows = []
    if rows:
        sys.stdout.write("\n".join(rows) + "\n")


if __name__ == "__main__":
    main()
