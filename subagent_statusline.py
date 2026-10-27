#!/usr/bin/env python3
"""Subagent status line: one row per visible subagent in the agent panel.

Row, left to right: status (▸ running, · pending, ✓ done, ✗ failed), label, model and
explicit effort, tokens and context gauge, description. The gauge is the main line's: the
same two ceilings, clamped to the task's own window.

Columns align across the rows of one payload while the panel is wide enough to keep
MIN_DESC cells of description; otherwise rows pack tightly. Parts drop from the right
when a row does not fit `columns`, which the harness declares (narrower than the terminal).

The gauge appears only once `contextWindowSize` is present, that is once the task's model
resolved (Claude Code 2.1.205+); ceilings from a guessed window would mislead, so until
then the row shows the muted token count alone.
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
    from statusline import (BOLD, GREEN, RED, RESET, VERSION, clean, context_part as gauge_part,
                            elide, human, muted, num, paint, tokens, visible_len)
except Exception:                                    # degrade to plain text, never disappear
    VERSION = "unknown"
    BOLD = RESET = ""
    GREEN = RED = ""

    def tokens(n):
        n = int(max(0, n))
        if n < 1_000:
            return str(n)
        if n < 1_000_000:
            return f"{n // 1_000}k"
        return f"{n // 100_000 / 10:.1f}M" if n < 10_000_000 else f"{n // 1_000_000}M"

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

    def gauge_part(used, window, width, count_cells=4):
        return tokens(used).rjust(count_cells) + " " * (width + 1)

# --- Tunables ----------------------------------------------------------------
BAR_W         = 10    # gauge cells; a multiple of 5 so the ordinary ceiling is a cell edge
NAME_MAX      = 22    # display cells for the label
META_MAX      = 16    # display cells for model and effort
MIN_DESC      = 20    # description cells kept before alignment is given up
FALLBACK_COLS = 80
# -------------------------------------------------------------------------------

MAX_TOKENS = 999_000_000
GAUGE_CELLS = 5 + BAR_W          # count right-aligned in 4 cells, space, gauge
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
    return {
        "status": str(task.get("status") or "").lower(),
        "label": elide(label, NAME_MAX),
        "meta": elide(f"{short_model(clean(task.get('model') or ''))} {shown}".strip(), META_MAX),
        "window": int(max(0.0, min(num(task.get("contextWindowSize"), 0), MAX_TOKENS))),
        "tokens": int(max(0.0, min(num(task.get("tokenCount"), 0), MAX_TOKENS))),
        "desc": clean(task.get("description") or "").strip(),
    }


def context_part(row: dict, aligned: bool) -> str:
    """Count and gauge; without a window, the muted count alone.

    Aligned, the part always takes GAUGE_CELLS so the description column lines up;
    otherwise it is as short as its content.
    """
    cells = GAUGE_CELLS - BAR_W - 1 if aligned else 0
    if row["window"] > 0:
        return gauge_part(row["tokens"], row["window"], BAR_W, cells)
    count = muted(tokens(row["tokens"]).rjust(cells)) if row["tokens"] > 0 else " " * cells
    return count + " " * (BAR_W + 1) if aligned else count


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
        if widths["context"]:
            parts.append(context_part(row, aligned=True))
    else:
        if row["meta"]:
            parts.append(muted(row["meta"]))
        if row["window"] > 0 or row["tokens"] > 0:
            parts.append(context_part(row, aligned=False))

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
        "context": any(r["window"] > 0 or r["tokens"] > 0 for r in rows_only),
    }
    needed = (2 + widths["label"] + MIN_DESC
              + (widths["meta"] + 2 if widths["meta"] else 0)
              + (GAUGE_CELLS + 2 if widths["context"] else 0) + 2)

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
