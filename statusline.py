#!/usr/bin/env python3
"""Claude Code status line: model, location, context gauge, rate limits, session totals.

The context segment is the token count and a gauge against two ceilings, not against the
context window: quality degrades in absolute terms, whatever the window size.
- The absolute ceiling is CEILING_ABS tokens, clamped to CEILING_FRAC of the real window so
  it stays below the point where Claude Code compacts. The gauge spans 0..absolute and is
  full only there.
- The ordinary ceiling is ORDINARY_FRAC of the absolute one (400k of 500k).
- Green below the ordinary ceiling, amber up to the absolute one, red from it. The count is
  floored, so it never shows a ceiling before the ceiling is crossed (399_600 -> "399k").
- Before the ordinary ceiling the gauge fills in half cells (█ ▌) over a ░ track. The last
  mile has less air: ▓ in whole cells over the same ░ track, one shade darker.

Rendering rules:
- Colour is state, never decoration: the theme's green, yellow and red by each metric's
  own thresholds, compared against the rounded value that is displayed. Red means alarm.
  Bars use a darker xterm-256 shade of their state (BAR) so they stay quiet next to the
  numbers. Secondary text uses explicit greys: Claude Code already dims the whole line, so
  SGR dim would carry no hierarchy. The model name alone is bold Claude orange.
- Gauge widths depend on COLUMNS alone (GAUGE_TIERS), so they never change between
  refreshes of the same terminal. Context widths are multiples of 5, which puts the
  ordinary ceiling on a cell edge. Only the 5h limit gets a gauge.
- The line is fitted to COLUMNS minus RESERVE: Claude Code draws the status line inside its
  own margins and truncates anything wider with an ellipsis.
- When the line does not fit, segments drop by priority: session name, lines changed,
  duration, cost, directory, rate limits. Model and context never drop.
  The session name goes first because Claude Code already shows a name set with --name
  or /rename on the prompt bar.
- Glyphs that common terminal fonts lack (◷ ⟳) are followed by a space: their fallback
  glyph can be wider than one cell and would otherwise overlap the next character.
- State colours are never bold: many terminals draw bold colours in their bright variant,
  which would split one state into two tones.

Failure containment: every optional segment is built behind its own guard, object fields
are read defensively, numbers are coerced to finite floats, and a failure in the core
path prints "ctx: n/a". Stdin and stdout are forced to UTF-8. `git status` runs at most
once per GIT_TTL seconds per session.

Input: the statusLine JSON on stdin. Output: one line. Python 3.7+, standard library only.
"""
import json
import math
import os
import subprocess
import sys
import tempfile
import time
import unicodedata
from datetime import datetime

# --- Tunables ----------------------------------------------------------------
VERSION       = "6.0.1"  # reported by --version; never drawn on the line
SESSION_MAX   = 20       # display cells allowed to the session name
MODEL_MAX     = 40       # display cells allowed to the model name (it never drops)
CEILING_ABS   = 500_000  # absolute context ceiling (tokens): red from here, the gauge's end
CEILING_FRAC  = 0.80     # ...never above this fraction of the real window
ORDINARY_FRAC = 0.80     # ordinary ceiling as a fraction of the absolute one: amber from here
LIMIT_WARN    = 60       # rate limit %: amber from here
LIMIT_HOT     = 85       # rate limit %: red, and the reset time appears
GIT_TTL       = 5.0      # seconds to reuse a cached `git status`
FALLBACK_COLS = 200      # assumed width when COLUMNS is absent
RESERVE       = 4        # columns Claude Code keeps for its margins (measured on 2.1.270)
# (minimum columns, context gauge cells, 5h limit gauge cells; 0 shows the 5h limit as text).
# Measured so wider gauges never cost a segment other than the session name, even for a
# heavy session: a 20-cell name, $87, 5h 30m, +3000 -900 lines and the reset time shown.
# Context widths are multiples of 5 so the ordinary ceiling lands on a cell edge.
GAUGE_TIERS   = ((187, 25, 10), (181, 25, 8), (175, 20, 6), (167, 20, 0), (163, 15, 0), (0, 10, 0))
# SGR parameters. States use the terminal theme's own palette; greys are fixed xterm-256.
GREEN, AMBER, RED = "32", "33", "31"
BAR = {GREEN: "38;5;28", AMBER: "38;5;136", RED: "38;5;124"}  # darker shade of each state
TRACK = {GREEN: "38;5;22", AMBER: "38;5;94", RED: "38;5;88"}  # one step darker: last-mile track
CLAUDE = "38;2;215;119;87"     # Claude Code's own "claude" theme colour, for the model name
SEP, MUTED = "38;5;240", "38;5;245"
# -------------------------------------------------------------------------------

RESET, BOLD = "\033[0m", "\033[1m"
PCT_MAX = 999
MAX_TOKENS = 999_000_000
TAIL = ("cost", "duration", "lines")
COUNT_CELLS = 4                # "999k", "1.2M", "999M": the gauge never shifts as it grows

# Keep the git subprocess from flashing a console window on Windows.
_NOWINDOW = {"creationflags": subprocess.CREATE_NO_WINDOW} if os.name == "nt" else {}


def num(x, default=0.0):
    """Coerce a JSON value to a finite float; anything else returns `default`, never raises."""
    try:
        v = float(x)
    except (TypeError, ValueError, OverflowError):     # OverflowError: float(10**400)
        return default
    return v if math.isfinite(v) else default


def human(n) -> str:
    """250_000 -> "250k", 999_500 -> "1M", 1_250_000 -> "1.25M"."""
    n = max(0, n)
    if n >= 999_500:
        return f"{n / 1_000_000:.2f}".rstrip("0").rstrip(".") + "M"
    if n >= 1_000:
        return f"{n / 1_000:.0f}k"
    return str(int(n))


def tokens(n) -> str:
    """Floored, so a ceiling never shows before it is crossed: 399_600 -> "399k", 1_250_000 -> "1.2M"."""
    n = int(max(0, n))
    if n < 1_000:
        return str(n)
    if n < 1_000_000:
        return f"{n // 1_000}k"
    if n < 10_000_000:
        return f"{n // 100_000 / 10:.1f}M"
    return f"{n // 1_000_000}M"


def visible_len(s: str) -> int:
    """Terminal display width: CSI sequences are zero-width, East Asian Wide/Fullwidth count 2."""
    out, i, n = 0, 0, len(s)
    while i < n:
        if s[i] == "\033" and i + 1 < n and s[i + 1] == "[":
            j = i + 2
            while j < n and not ("@" <= s[j] <= "~"):
                j += 1
            i = j + 1
        else:
            out += 2 if unicodedata.east_asian_width(s[i]) in ("W", "F") else 1
            i += 1
    return out


def elide(s: str, budget: int) -> str:
    """Trim s to at most `budget` display cells, marking the cut with an ellipsis."""
    if budget <= 0:
        return ""
    if visible_len(s) <= budget:
        return s
    out = ""
    for ch in s:
        if visible_len(out + ch) > budget - 1:
            break
        out += ch
    return out + "…"


def clean(s) -> str:
    """Replace control, format and line/paragraph separator characters with spaces."""
    return "".join(" " if unicodedata.category(ch) in ("Cc", "Cf", "Zl", "Zp") else ch for ch in str(s))


def short_dir(path: str) -> str:
    """Home-relative, forward-slashed path for display (~/projects/webapp)."""
    if not path:
        return ""
    p = path.replace("\\", "/").rstrip("/")
    home = (os.path.expanduser("~") or "").replace("\\", "/").rstrip("/")
    if home and p.lower() == home.lower():
        return "~"
    if home and p.lower().startswith(home.lower() + "/"):
        return "~/" + p[len(home) + 1:]
    return p


def obj(d, key: str) -> dict:
    """d[key] when it is a mapping, else {}: a malformed field degrades to an empty segment."""
    v = d.get(key) if isinstance(d, dict) else None
    return v if isinstance(v, dict) else {}


def fmt_duration(ms: float) -> str:
    s = int(ms // 1000)
    h, rem = divmod(s, 3600)
    m = rem // 60
    if h:
        return f"{h}h {m:02d}m"
    if m:
        return f"{m}m"
    return f"{s}s"


# --- Styling -------------------------------------------------------------------

def paint(s: str, sgr: str, bold: bool = False) -> str:
    return f"\033[{'1;' if bold else ''}{sgr}m{s}{RESET}" if s else ""


def muted(s: str) -> str:
    return paint(s, MUTED)


def state_colour(pct: int, warn: int, hot: int) -> str:
    return RED if pct >= hot else AMBER if pct >= warn else GREEN


def gauge(frac: float, width: int, colour: str) -> str:
    """`width` cells of █ (used) and ░ (free) in the bar shade of the state colour."""
    fill = round(max(0.0, min(1.0, frac)) * width)
    return paint("█" * fill + "░" * (width - fill), BAR.get(colour, colour))


def context_gauge(used: int, ceiling: int, width: int, colour: str) -> str:
    """0..ceiling in two zones split at the ordinary ceiling (a cell edge: width is a multiple of 5).

    Before it: half-cell fill █ ▌ on a ░ track. The last mile has less air: ▓ in whole cells,
    on the same ░ track one shade darker (TRACK), so the gauge stays one solid rectangle.
    The bar is full only at the ceiling itself, so a full bar always means red.
    """
    notch = round(width * ORDINARY_FRAC)
    ordinary = max(1, int(ceiling * ORDINARY_FRAC))
    track = width - notch
    if used >= ceiling:
        cells, track = "█" * notch + "▓" * (width - notch), 0
    elif used < ordinary:
        full, half = divmod(used * 2 * width // ceiling, 2)
        cells = "█" * full + ("▌" if half else "")
        cells += "░" * (notch - len(cells))
    else:
        last = max(0, min((used - ordinary) * (width - notch) // max(1, ceiling - ordinary),
                          width - notch - 1))
        cells, track = "█" * notch + "▓" * last, width - notch - last
    return paint(cells, BAR.get(colour, colour)) + paint("░" * track, TRACK.get(colour, colour))


def context_part(used: int, window: int, width: int, count_cells: int = COUNT_CELLS) -> str:
    """Count and gauge against the two ceilings of `window` (0: unknown, the absolute ceiling).

    The count is right-aligned in `count_cells` (0: unpadded) so a column of them lines up.
    """
    ceiling = max(1, min(CEILING_ABS, int(window * CEILING_FRAC)) if window else CEILING_ABS)
    ordinary = max(1, int(ceiling * ORDINARY_FRAC))
    colour = GREEN if used < ordinary else AMBER if used < ceiling else RED
    count = paint(tokens(used).rjust(count_cells), colour)
    return f"{count} {context_gauge(used, ceiling, width, colour)}"


def gauge_widths(cols: int):
    """(context gauge cells, 5h limit gauge cells) for a terminal `cols` wide."""
    return next(((ctx, limit) for min_cols, ctx, limit in GAUGE_TIERS if cols >= min_cols),
                GAUGE_TIERS[-1][1:])


# --- Git -----------------------------------------------------------------------

def _git(cwd: str, *args: str):
    try:
        return subprocess.run(
            ["git", "-C", cwd, "--no-optional-locks", *args],
            capture_output=True, encoding="utf-8", errors="replace",
            timeout=1.0, **_NOWINDOW,
        )
    except Exception:
        return None


def _git_status_live(cwd: str):
    """{branch, dirty, ahead, behind} for cwd, or None if not a repo / no git."""
    r = _git(cwd, "status", "--porcelain=v1", "--branch")
    if r is None or r.returncode != 0 or not r.stdout:
        return None

    lines = r.stdout.splitlines()
    head = lines[0] if lines else ""
    dirty = len(lines) > 1
    branch, ahead, behind = "", 0, 0

    if head.startswith("## "):
        info = head[3:]
        for prefix in ("No commits yet on ", "Initial commit on "):     # unborn branch
            if info.startswith(prefix):
                branch = info[len(prefix):].strip().split(" ")[0]
                break
        # Detached HEAD is exactly "HEAD (no branch)"; a branch named "HEADx" is legal.
        if not branch and info != "HEAD" and not info.startswith("HEAD ("):
            branch = info.split(" ")[0].split("...")[0]
        if "[" in head and "]" in head:                                  # [ahead N, behind M]
            for part in head[head.index("[") + 1:head.index("]")].split(","):
                part = part.strip()
                if part.startswith("ahead "):
                    ahead = int(num(part[6:], 0))
                elif part.startswith("behind "):
                    behind = int(num(part[7:], 0))

    if not branch:                                                       # detached HEAD
        r2 = _git(cwd, "rev-parse", "--short", "HEAD")
        if r2 is not None and r2.returncode == 0:
            branch = r2.stdout.strip()

    return {"branch": branch, "dirty": dirty, "ahead": ahead, "behind": behind} if branch else None


def git_info(cwd: str, session_id: str):
    """Git state cached per session for GIT_TTL seconds, invalidated when cwd changes.

    The cache file is written atomically under a pid-unique name; a corrupt cache reads
    as a miss and heals on the next refresh.
    """
    if not cwd or not os.path.isdir(cwd):
        return None
    sid = "".join(ch for ch in str(session_id) if ch.isalnum() or ch in "._-")[:64]
    if not sid:
        return _git_status_live(cwd)

    cache = os.path.join(tempfile.gettempdir(), f"claude-statusline-git-{sid}.json")
    try:
        with open(cache, "r", encoding="utf-8") as f:
            c = json.load(f)
        if c.get("cwd") == cwd and time.time() - os.path.getmtime(cache) < GIT_TTL:
            return c.get("info")
    except Exception:
        pass

    info = _git_status_live(cwd)
    tmp = f"{cache}.{os.getpid()}.tmp"
    try:
        with open(tmp, "w", encoding="utf-8") as f:
            json.dump({"cwd": cwd, "info": info}, f)
        os.replace(tmp, cache)
    except Exception:
        try:
            os.remove(tmp)
        except OSError:
            pass
    return info


# --- Line ------------------------------------------------------------------------

def _join(segments) -> str:
    separator = f" {paint('│', SEP)} "
    out, prev = "", None
    for _, key, text in segments:
        if prev is not None:
            if key in TAIL and prev in TAIL:
                out += "  "                    # cost, duration and lines read as one group
            else:
                out += separator
        out += text
        prev = key
    return out


def render(d: dict) -> str:
    if not isinstance(d, dict):
        d = {}
    try:
        cols = max(0, int(os.environ.get("COLUMNS") or FALLBACK_COLS))
    except Exception:
        cols = FALLBACK_COLS
    ctx_w, limit_w = gauge_widths(cols)

    cw = obj(d, "context_window")
    cost = obj(d, "cost")
    cwd = obj(d, "workspace").get("current_dir") or d.get("cwd") or ""

    used = num(cw.get("total_input_tokens"), 0)
    if used <= 0:     # Claude Code < 2.1.132 reported cumulative totals; current_usage covers it
        cu = obj(cw, "current_usage")
        used = sum(num(cu.get(k), 0) for k in
                   ("input_tokens", "cache_creation_input_tokens", "cache_read_input_tokens"))
    used = int(max(0.0, min(used, MAX_TOKENS)))
    window = int(max(0.0, min(num(cw.get("context_window_size"), 0), MAX_TOKENS)))

    # --- core segments: never dropped ------------------------------------------
    model = elide(clean(obj(d, "model").get("display_name") or "Claude"), MODEL_MAX)
    effort = obj(d, "effort").get("level")
    model_seg = paint(model, CLAUDE, bold=True) + (f" {muted(clean(effort))}" if effort else "")
    ctx_seg = context_part(used, window, ctx_w)

    # --- optional segments: bad data drops the segment, never the line ----------
    def build_session() -> str:
        name = clean(d.get("session_name") or "").strip()
        return muted(elide(name, SESSION_MAX)) if name else ""

    def build_location() -> str:
        seg = short_dir(clean(cwd))
        if not seg:
            return ""
        git = git_info(cwd, d.get("session_id") or "")
        if git:
            inner = muted(clean(git["branch"])) + (f"{BOLD}*{RESET}" if git["dirty"] else "")
            marks = (f"↑{git['ahead']}" if git["ahead"] else "") + \
                    (f"↓{git['behind']}" if git["behind"] else "")
            if marks:
                inner += " " + muted(marks)
            seg += f" {muted('(')}{inner}{muted(')')}"
        return seg

    def build_limits() -> str:
        rl = obj(d, "rate_limits")
        parts = []
        for tag, key in (("5h", "five_hour"), ("7d", "seven_day")):
            w = obj(rl, key)                     # one malformed window drops only itself
            raw = num(w.get("used_percentage"), None)
            if raw is None:
                continue
            p = max(0, min(PCT_MAX, round(raw)))
            c = state_colour(p, LIMIT_WARN, LIMIT_HOT)
            seg = muted(tag) + " "
            if limit_w and tag == "5h":      # 7d barely moves within a session: text is enough
                seg += gauge(p / 100, limit_w, c) + " "
            seg += paint(f"{p}%", c)
            if tag == "5h" and p >= LIMIT_HOT:
                try:
                    resets = int(num(w.get("resets_at"), 0))
                    if resets > 0:
                        seg += " " + paint("⟳ " + datetime.fromtimestamp(resets).strftime("%H:%M"), c)
                except (OverflowError, OSError, ValueError):
                    pass
            parts.append(seg)
        return ("  " if limit_w else " ").join(parts)

    def build_cost() -> str:
        return muted(f"${max(0.0, num(cost.get('total_cost_usd'), 0.0)):.2f}")

    def build_duration() -> str:
        ms = num(cost.get("total_duration_ms"), 0)
        return muted("◷ " + fmt_duration(ms)) if ms > 0 else ""

    def build_lines() -> str:
        added = int(max(0.0, num(cost.get("total_lines_added"), 0)))
        removed = int(max(0.0, num(cost.get("total_lines_removed"), 0)))
        return muted(f"+{added} −{removed}") if (added or removed) else ""

    def safe(build) -> str:
        try:
            return build()
        except Exception:
            return ""

    # (drop priority, key, text) in display order; the highest priority drops first
    segments = [
        (0, "model", model_seg),
        (2, "location", safe(build_location)),
        (6, "session", safe(build_session)),
        (0, "context", ctx_seg),
        (1, "limits", safe(build_limits)),
        (3, "cost", safe(build_cost)),
        (4, "duration", safe(build_duration)),
        (5, "lines", safe(build_lines)),
    ]
    segments = [s for s in segments if s[2]]

    while True:
        line = _join(segments)
        if visible_len(line) <= cols - RESERVE or all(p == 0 for p, _, _ in segments):
            return line                    # model + gauge alone may exceed; the harness truncates
        worst = max(p for p, _, _ in segments)
        for i in range(len(segments) - 1, -1, -1):          # rightmost of the lowest importance
            if segments[i][0] == worst:
                del segments[i]
                break


def main() -> None:
    for stream in (sys.stdin, sys.stdout):     # never depend on -X utf8
        try:
            stream.reconfigure(encoding="utf-8", errors="replace")
        except Exception:
            pass
    if "--version" in sys.argv[1:]:
        sys.stdout.write(f"statusline {VERSION}\n")
        return
    try:
        d = json.load(sys.stdin)
    except Exception:
        d = {}
    try:
        line = render(d)
    except Exception:
        line = "ctx: n/a"
    sys.stdout.write(line + "\n")


if __name__ == "__main__":
    main()
