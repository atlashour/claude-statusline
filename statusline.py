#!/usr/bin/env python3
"""Claude Code status line v5 — adaptive context-saturation gauge.

Design rationale (v5 over v4):

A. VERSIONED. `--version` prints the build a machine carries, so a
   fleet can be audited over ssh. It is deliberately NOT painted on the
   line: a version sitting next to the model name reads as the model's
   version. Bump VERSION when this file changes.

B. SESSION IDENTITY. Renders `session_name`, the handle other sessions
   address this one by via SendMessage. Note the harness only fills
   that field for a session named with --name or /rename, or one with
   an AI-generated title: the default display name (my-app-3f) does NOT
   populate it, so name your sessions if you want this segment. Elided
   to SESSION_MAX cells because generated titles are whole sentences.

C. HARDER FAILURE CONTAINMENT. Object-typed fields are read through
   obj(), so a field arriving as a bare string or list degrades to an
   empty segment instead of raising past the optional-segment guards
   (v4 raised on, e.g., a string "model" and fell back to "ctx: n/a").

A companion script, subagent_statusline.py, renders the agent panel's
rows in the same visual language and imports its helpers from here.
The dependency points that way on purpose: this file stays
self-contained, because it is the one that must never fail.

Design rationale (v4 over v3):

1. CORRECTNESS — the comfort ceiling is *window-aware*, not model-biased.
   v3 used a fixed COMFORT=250k, which exceeds a 200k context window: on
   200k-window sessions the gauge could never reach red before the hard
   limit forced a compaction. v4 clamps the effective budget to
   min(COMFORT_ABS, 80% of the reported window). On a 1M window the
   absolute 250k ceiling governs (context rot is absolute, not
   proportional); on a 200k window the clamp fires red at 160k. The same
   file is therefore correct on any machine regardless of which model or
   window size it runs.

2. PERFORMANCE — `git status` is cached per session (GIT_TTL seconds,
   keyed by a sanitized session_id per the official docs' recommendation)
   instead of running on every render. The cache invalidates when cwd
   changes; dirty/ahead/behind may lag up to GIT_TTL seconds by design.
   Cache files live in the OS temp dir and are cleaned by the OS.

3. NEW VITAL DATA — subscription rate limits (5 h / 7 d windows) with
   threshold colours and the local reset time once the 5 h window passes
   LIMIT_HOT %; session duration; lines added/removed.

4. WIDTH-ADAPTIVE — reads the COLUMNS env var (injected by Claude Code
   v2.1.153+; without it FALLBACK_COLS assumes a wide terminal, because
   the docs state the real size is unreadable from inside the script) and
   drops the least-important segments first so the line never wraps:
   lines± → duration → cost → git/dir → limits. Model and the context
   gauge never drop; as a last resort the gauge sheds its dim
   window-fill suffix. Width counting is ANSI-aware and counts
   East-Asian Wide/Fullwidth characters (e.g. CJK directory names) as 2
   cells; ambiguous-width glyphs count as 1, matching the default of
   Terminal.app/iTerm2 (a terminal configured ambiguous-as-wide will
   render slightly wider than computed).

Input: the statusLine JSON on stdin (model.display_name, effort.level,
context_window.*, cost.*, workspace.current_dir, rate_limits.*,
session_id). context_window.total_input_tokens reflects the live window
on Claude Code >= 2.1.132 (older versions reported cumulative session
totals there; the current_usage fallback covers them). External calls:
at most two short git commands per cache refresh (`status`, plus
`rev-parse` only when HEAD is detached), both with a 1 s timeout.

Failure containment is layered: each optional segment (git/dir, limits,
cost, duration, lines±) is built inside its own guard and silently
disappears if its data is malformed — one bad field cannot erase the
gauge; a failure in the core path degrades to "ctx: n/a"; stdout is
forced to UTF-8 (Windows defaults to cp1252, which would otherwise raise
on the bar/separator glyphs).

No third-party dependencies; Python 3.7+ (tested on 3.9).
"""
import json
import os
import subprocess
import sys
import tempfile
import time
from datetime import datetime
from unicodedata import east_asian_width

# --- Tunables (edit these) ------------------------------------------------
VERSION       = "5.0.0"  # reported by --version; never drawn on the line
SESSION_MAX   = 20       # display cells allowed to session_name before eliding
COMFORT_ABS   = 250_000  # absolute mental-saturation ceiling (tokens)
COMFORT_FRAC  = 0.80     # ...but never above this fraction of the real window
WARN_FRAC     = 0.60     # caution band starts at this fraction of the budget
BAR_W         = 12       # gauge width in cells
GIT_TTL       = 5.0      # seconds to reuse a cached `git status`
LIMIT_WARN    = 60       # rate-limit %: yellow from here
LIMIT_HOT     = 85       # rate-limit %: red + show reset time from here
FALLBACK_COLS = 200      # assumed width when COLUMNS is absent (favours info)
# ---------------------------------------------------------------------------

A = {
    "reset": "\033[0m", "dim": "\033[2m", "bold": "\033[1m",
    "green": "\033[32m", "yellow": "\033[33m", "red": "\033[31m",
    "cyan": "\033[36m", "gray": "\033[90m",
}

# Keep the git subprocess from flashing a console window on Windows.
_NOWINDOW = {"creationflags": subprocess.CREATE_NO_WINDOW} if os.name == "nt" else {}


def num(x, default=0.0):
    """Coerce a JSON value to float; malformed types fall back, never raise."""
    try:
        return float(x)
    except (TypeError, ValueError):
        return default


def human(n: int) -> str:
    # 1_000_000 → "1M", 1_250_000 → "1.25M", 250_000 → "250k" (no ragged ".00M").
    if n >= 1_000_000:
        return f"{n / 1_000_000:.2f}".rstrip("0").rstrip(".") + "M"
    if n >= 1_000:
        return f"{n / 1_000:.0f}k"
    return str(int(n))


def visible_len(s: str) -> int:
    """Terminal display width of s.

    CSI escape sequences (ESC [ ... final-byte) are zero-width; East-Asian
    Wide/Fullwidth characters count 2 cells; everything else counts 1.
    """
    out, i, n = 0, 0, len(s)
    while i < n:
        ch = s[i]
        if ch == "\033" and i + 1 < n and s[i + 1] == "[":
            j = i + 2
            while j < n and not ("@" <= s[j] <= "~"):    # CSI ends at any final byte
                j += 1
            i = j + 1
        else:
            out += 2 if east_asian_width(ch) in ("W", "F") else 1
            i += 1
    return out


def elide(s: str, budget: int) -> str:
    """Trim s to at most `budget` display cells, marking the cut with an ellipsis.

    Measured in cells rather than characters so a CJK session name is
    trimmed to the width it actually occupies, matching visible_len().
    """
    if budget <= 0:
        return ""
    if visible_len(s) <= budget:
        return s
    out = ""
    for ch in s:
        if visible_len(out + ch) > budget - 1:       # reserve a cell for "…"
            break
        out += ch
    return out + "…"


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
    dirty = len(lines) > 1            # any porcelain entry (incl. untracked)
    branch, ahead, behind = "", 0, 0

    if head.startswith("## "):
        info = head[3:]
        # Unborn branch (no commits yet); older gits said "Initial commit on".
        for prefix in ("No commits yet on ", "Initial commit on "):
            if info.startswith(prefix):
                branch = info[len(prefix):].strip().split(" ")[0]
                break
        # Detached HEAD is exactly "HEAD (no branch)" — a branch literally
        # named e.g. "HEADx" is legal and must NOT be treated as detached.
        if not branch and info != "HEAD" and not info.startswith("HEAD ("):
            branch = info.split(" ")[0].split("...")[0]
        if "[" in head and "]" in head:                  # [ahead N, behind M]
            for part in head[head.index("[") + 1:head.index("]")].split(","):
                part = part.strip()
                if part.startswith("ahead "):
                    ahead = int(num(part[6:], 0))
                elif part.startswith("behind "):
                    behind = int(num(part[7:], 0))

    if not branch:                                       # detached HEAD → sha
        r2 = _git(cwd, "rev-parse", "--short", "HEAD")
        if r2 is not None and r2.returncode == 0:
            branch = r2.stdout.strip()

    return {"branch": branch, "dirty": dirty, "ahead": ahead, "behind": behind} if branch else None


def git_info(cwd: str, session_id: str):
    """Cached git state: at most one live `git status` every GIT_TTL seconds.

    Keyed by session_id (stable within a session, unique across sessions,
    per the docs), sanitized before use in a filename. Invalidated when
    cwd changes mid-session. The temp file is written atomically under a
    pid-unique name so overlapping renders cannot interleave writes; a
    corrupted cache reads as a miss and self-heals on the next refresh.
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
    try:
        tmp = f"{cache}.{os.getpid()}.tmp"
        with open(tmp, "w", encoding="utf-8") as f:
            json.dump({"cwd": cwd, "info": info}, f)
        os.replace(tmp, cache)
    except Exception:
        pass
    return info


def limit_col(pct: float) -> str:
    if pct >= LIMIT_HOT:
        return A["red"]
    if pct >= LIMIT_WARN:
        return A["yellow"]
    return A["green"]


def fmt_duration(ms: float) -> str:
    s = int(ms // 1000)
    h, rem = divmod(s, 3600)
    m = rem // 60
    if h:
        return f"{h}h {m:02d}m"
    if m:
        return f"{m}m"
    return f"{s}s"


def obj(d: dict, key: str) -> dict:
    """d[key] when it is a mapping, else {}.

    The harness documents these fields as objects, but a future rename or a
    truncated payload can deliver a bare string or a list; treating those as
    empty keeps one malformed field from erasing the whole line.
    """
    v = d.get(key) if isinstance(d, dict) else None
    return v if isinstance(v, dict) else {}


def render(d: dict) -> str:
    if not isinstance(d, dict):
        d = {}
    model = obj(d, "model").get("display_name") or "Claude"
    effort = obj(d, "effort").get("level")
    cw = obj(d, "context_window")
    cost_o = obj(d, "cost")
    cwd = obj(d, "workspace").get("current_dir") or d.get("cwd") or ""
    session_id = d.get("session_id") or ""
    session_name = d.get("session_name") or ""

    used = int(num(cw.get("total_input_tokens"), 0))
    if not used:
        cu = obj(cw, "current_usage")
        used = int(
            num(cu.get("input_tokens"), 0)
            + num(cu.get("cache_creation_input_tokens"), 0)
            + num(cu.get("cache_read_input_tokens"), 0)
        )

    window = int(num(cw.get("context_window_size"), 0))
    wp = num(cw.get("used_percentage"), None)
    if wp is None:
        win_pct = round(used / window * 100) if window else 0
    else:
        win_pct = round(wp)

    # Window-aware comfort budget: the absolute ceiling, clamped so red always
    # fires before the hard window limit (v3 bug: 250k > a 200k window).
    comfort = min(COMFORT_ABS, int(window * COMFORT_FRAC)) if window else COMFORT_ABS
    warn = int(comfort * WARN_FRAC)

    if used >= comfort:
        col = A["red"]
    elif used >= warn:
        col = A["yellow"]
    else:
        col = A["green"]

    frac = used / comfort if comfort else 0.0
    fill = min(BAR_W, round(frac * BAR_W))
    bar = "█" * fill + "░" * (BAR_W - fill)
    sat_pct = round(frac * 100)
    flag = "  ◬ saturated" if used >= comfort else ""

    # --- core segments (never dropped; guarded only by main's catch-all) --
    model_seg = f"{A['bold']}{A['cyan']}{model}{A['reset']}"
    if effort:
        model_seg += f" {A['dim']}{effort}{A['reset']}"

    ctx_min = f"{col}ctx {human(used)}/{human(comfort)} {bar} {sat_pct}%{flag}{A['reset']}"
    ctx_seg = ctx_min
    if window:
        ctx_seg += f"{A['dim']} · {win_pct}% of {human(window)}{A['reset']}"

    # --- optional segments: each guarded so bad data drops the segment, ---
    # --- never the line -----------------------------------------------------
    def build_session() -> str:
        """The session's own name — the handle other sessions address it by.

        Absent unless the session was named with --name or /rename, or has an
        AI-generated title: the default display name (e.g. my-app-3f) does not
        populate this field. Elided because generated titles are sentences.
        """
        name = str(session_name).strip()
        return f"{A['dim']}{elide(name, SESSION_MAX)}{A['reset']}" if name else ""

    def build_loc() -> str:
        dir_disp = short_dir(cwd)
        if not dir_disp:
            return ""
        seg = dir_disp
        git = git_info(cwd, session_id)
        if git:
            inner = f"{A['dim']}{git['branch']}{A['reset']}"
            if git["dirty"]:
                inner += f"{A['yellow']}*{A['reset']}"
            div = ""
            if git["ahead"]:
                div += f"{A['dim']}↑{git['ahead']}{A['reset']}"
            if git["behind"]:
                div += f"{A['dim']}↓{git['behind']}{A['reset']}"
            if div:
                inner += " " + div
            seg += f" {A['dim']}({A['reset']}{inner}{A['dim']}){A['reset']}"
        return seg

    def build_limits() -> str:
        rl = d.get("rate_limits") or {}
        parts = []
        for tag, key in (("5h", "five_hour"), ("7d", "seven_day")):
            w = rl.get(key) or {}
            pct = num(w.get("used_percentage"), None)
            if pct is None:
                continue
            seg = f"{limit_col(pct)}{tag} {pct:.0f}%{A['reset']}"
            if tag == "5h" and pct >= LIMIT_HOT:
                resets = int(num(w.get("resets_at"), 0))
                if resets > 0:
                    t = datetime.fromtimestamp(resets).strftime("%H:%M")
                    seg += f"{A['dim']} ⟳{t}{A['reset']}"
            parts.append(seg)
        return " ".join(parts)

    def build_cost() -> str:
        return f"{A['dim']}${num(cost_o.get('total_cost_usd'), 0.0):.2f}{A['reset']}"

    def build_dur() -> str:
        ms = num(cost_o.get("total_duration_ms"), 0)
        return f"{A['dim']}◷ {fmt_duration(ms)}{A['reset']}" if ms else ""

    def build_lines() -> str:
        la = int(num(cost_o.get("total_lines_added"), 0))
        lr = int(num(cost_o.get("total_lines_removed"), 0))
        if not (la or lr):
            return ""
        return f"{A['green']}+{la}{A['reset']} {A['red']}−{lr}{A['reset']}"

    def safe(build) -> str:
        try:
            return build()
        except Exception:
            return ""

    # priority: 0 model+ctx (never dropped) · 1 limits · 2 session/git/dir
    #           · 3 cost · 4 duration · 5 lines±  — higher number is dropped first
    ordered = [                          # (priority, key, segment) in display order
        (0, "model", model_seg),
        (2, "sess", safe(build_session)),
        (2, "loc", safe(build_loc)),
        (0, "ctx", ctx_seg),
        (1, "limits", safe(build_limits)),
        (3, "cost", safe(build_cost)),
        (4, "dur", safe(build_dur)),
        (5, "lines", safe(build_lines)),
    ]
    ordered = [(p, k, s) for p, k, s in ordered if s]

    # --- width-adaptive assembly: drop least-important segments until it fits
    try:
        cols = int(os.environ.get("COLUMNS") or FALLBACK_COLS)
    except Exception:
        cols = FALLBACK_COLS

    sep = f" {A['gray']}│{A['reset']} "
    while True:
        line = sep.join(s for _, _, s in ordered)
        if visible_len(line) <= cols:
            return line
        if all(p == 0 for p, _, _ in ordered):
            break
        worst = max(p for p, _, _ in ordered)
        for i in range(len(ordered) - 1, -1, -1):        # rightmost of the worst
            if ordered[i][0] == worst:
                del ordered[i]
                break

    # Last resort: shed the gauge's dim window-fill suffix. If even this
    # exceeds cols, return it anyway — the harness truncates the tail.
    return sep.join(ctx_min if k == "ctx" else s for _, k, s in ordered)


def main() -> None:
    try:
        sys.stdout.reconfigure(encoding="utf-8", errors="replace")
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
