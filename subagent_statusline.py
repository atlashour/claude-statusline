#!/usr/bin/env python3
"""Subagent status line: one row per visible subagent in the agent panel.

Row, left to right: status (▸ running, · pending, ✓ done, ✗ failed), label, model and
explicit effort, context gauge against the task's own window, tokens, description.

Columns align across the rows of one payload while the panel is wide enough to keep
MIN_DESC cells of description; otherwise rows pack tightly. Parts drop from the right
when a row does not fit `columns`, which the harness declares (narrower than the terminal).

The gauge appears only once `contextWindowSize` is present, that is once the task's model
resolved (Claude Code 2.1.205+); a percentage against a guessed window would mislead.
Effort appears only when set explicitly (2.1.214+); absent means inherited.

Styling comes from statusline.py so both share one visual language. If that import fails,
rows still render as plain text.

Input: JSON on stdin with `columns` and `tasks`. Output: one JSON line per task with an id,
{"id": ..., "content": ...}; tasks without an id keep their default rendering.
Python 3.7+, standard library only.
"""
import json
import math
import os
import sys

sys.path.insert(0, os.path.dirname(os.path.abspath(__file__)))

try:
    from statusline import (BOLD, GREEN, RED, RESET, VERSION, clean, elide, gauge, human,
                            muted, num, paint, state_colour, visible_len)
except Exception:                                    # degrade to plain text, never disappear
    VERSION = "unknown"
    BOLD = RESET = ""
    GREEN = RED = ""

    def num(x, default=0.0):
        try:
            v = float(x)
        except (TypeError, ValueError, OverflowError):
            return default
        return v if math.isfinite(v) else default

    def human(n):
        return f"{int(n) // 1000}k" if n >= 1000 else str(int(n))

    def visible_len(s):
        return len(s)

    def elide(s, budget):
        return s if len(s) <= budget else s[:max(0, budget - 1)] + "…"

    def clean(s):
        return "".join(ch if ch.isprintable() else " " for ch in str(s))

    def paint(s, colour, bold=False):
        return s

    def muted(s):
        return s

    def gauge(frac, width, colour):
        return ""

    def state_colour(pct, warn, hot):
        return ""

# --- Tunables ----------------------------------------------------------------
BAR_W         = 8     # gauge cells
WARN_PCT      = 60    # gauge turns amber at this % of the task's window
HOT_PCT       = 85    # ...and red here
NAME_MAX      = 22    # display cells for the label
META_MAX      = 16    # display cells for model and effort
MIN_DESC      = 20    # description cells kept before alignment is given up
FALLBACK_COLS = 80
# -------------------------------------------------------------------------------

PCT_MAX = 999
MAX_TOKENS = 999_000_000
GAUGE_CELLS = BAR_W + 7          # gauge, space, "100%", space, "◬"
STATUS = {"running": ("▸", None), "pending": ("·", None), "queued": ("·", None),
          "completed": ("✓", GREEN), "done": ("✓", GREEN),
          "failed": ("✗", RED), "error": ("✗", RED)}


def short_model(model_id: str) -> str:
    """claude-opus-5 -> opus-5; claude-haiku-4-5-20251001 -> haiku-4-5."""
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


def describe(task: dict) -> dict:
    """The display values of one task, each bounded so no sibling can skew the columns."""
    label = clean(task.get("label") or task.get("name") or "").strip() or "agent"
    effort = task.get("effort")
    if effort is None or isinstance(effort, bool):
        shown = ""
    elif isinstance(effort, (int, float)):
        budget = num(effort, 0)
        shown = human(min(budget, MAX_TOKENS)) if budget > 0 else ""
    else:
        shown = clean(effort).strip()
    tokens = max(0.0, min(num(task.get("tokenCount"), 0), MAX_TOKENS))
    return {
        "status": str(task.get("status") or "").lower(),
        "label": elide(label, NAME_MAX),
        "meta": elide(f"{short_model(clean(task.get('model') or ''))} {shown}".strip(), META_MAX),
        "window": max(0.0, num(task.get("contextWindowSize"), 0)),
        "tokens": tokens,
        "tokens_txt": human(tokens) if tokens > 0 else "",
        "desc": clean(task.get("description") or "").strip(),
    }


def context_part(row: dict) -> str:
    pct = max(0, min(PCT_MAX, round(row["tokens"] / row["window"] * 100)))
    colour = state_colour(pct, WARN_PCT, HOT_PCT)
    mark = " " + paint("◬", RED) if pct >= 100 else "  "
    return f"{gauge(pct / 100, BAR_W, colour)} {paint(f'{pct:>3}%', colour)}{mark}"


def pad(s: str, width: int) -> str:
    return s + " " * max(0, width - visible_len(s))


def render_row(row: dict, columns: int, widths=None) -> str:
    """One row trimmed to `columns` cells; `widths` aligns it with its siblings."""
    glyph, colour = STATUS.get(row["status"], ("·", None))
    head = f"{glyph if colour is None else paint(glyph, colour)} {BOLD}{row['label']}{RESET}"
    parts = []
    if widths:
        head = pad(head, widths["label"] + 2)
        if widths["meta"]:
            parts.append(muted(pad(row["meta"], widths["meta"])))
        if widths["gauge"]:
            parts.append(context_part(row) if row["window"] > 0 else " " * GAUGE_CELLS)
        if widths["tokens"]:
            parts.append(muted(row["tokens_txt"].rjust(widths["tokens"])))
    else:
        if row["meta"]:
            parts.append(muted(row["meta"]))
        if row["window"] > 0:
            parts.append(context_part(row).rstrip())
        if row["tokens_txt"]:
            parts.append(muted(row["tokens_txt"]))

    while True:
        line = "  ".join([head] + parts)
        room = columns - visible_len(line) - 2
        if row["desc"] and room > 4:
            line += "  " + muted(elide(row["desc"], room))
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
    tasks = d.get("tasks") if isinstance(d.get("tasks"), list) else []

    described = []
    for task in tasks:
        if not isinstance(task, dict) or not task.get("id"):
            continue                       # no id: the harness keeps its default row
        try:
            described.append((str(task["id"]), describe(task)))
        except Exception:
            continue                       # one bad task drops its row only

    rows_only = [r for _, r in described]
    widths = {
        "label": max((visible_len(r["label"]) for r in rows_only), default=0),
        "meta": max((visible_len(r["meta"]) for r in rows_only), default=0),
        "gauge": any(r["window"] > 0 for r in rows_only),
        "tokens": max((len(r["tokens_txt"]) for r in rows_only), default=0),
    }
    needed = (2 + widths["label"] + MIN_DESC
              + (widths["meta"] + 2 if widths["meta"] else 0)
              + (GAUGE_CELLS + 2 if widths["gauge"] else 0)
              + (widths["tokens"] + 2 if widths["tokens"] else 0) + 2)

    rows = []
    for tid, row in described:
        try:
            content = render_row(row, columns, widths if columns >= needed else None)
        except Exception:
            continue
        rows.append(json.dumps({"id": tid, "content": content}, ensure_ascii=False))
    return rows


def main() -> None:
    for stream in (sys.stdin, sys.stdout):     # never depend on -X utf8
        try:
            stream.reconfigure(encoding="utf-8", errors="replace")
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
