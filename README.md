# claude-statusline

A status line for Claude Code built around one number: how close the session is to the
point where the model starts losing the thread.

## Why the gauge is not the context window

Most status lines plot usage against the model's context window, so a 1M window sits at
a comfortable 20% while the answers have already gone vague. Context rot is absolute,
not proportional. Quality degrades somewhere past 200k tokens whether the window holds
200k or a million.

So the gauge measures usage against two ceilings instead. Up to 400k tokens is ordinary
work. From 400k the session is on its last mile, and 500k is the wall. The gauge spans
0 to 500k: green below 400k, yellow on the last mile, red and full from 500k. Its last
fifth is drawn with less air, `▓` over `⣿`, so the last mile reads at a glance.

Both ceilings are capped by the real window, so they never promise room the window does
not have: on a 200k window the wall sits at 160k (80% of the window, before compaction is
forced on you) and the last mile starts at 128k.

The segment is just the token count and the gauge. The count is rounded down, so it
never shows `400k` before 400k is reached.

## Install

Requires Python 3.7 or later and Claude Code 2.1.153 or later. No dependencies.

Clone the repository, then point `~/.claude/settings.json` at it:

```json
{
  "statusLine": {
    "type": "command",
    "command": "python -X utf8 /path/to/claude-statusline/statusline.py",
    "padding": 0,
    "refreshInterval": 10
  },
  "subagentStatusLine": {
    "type": "command",
    "command": "python -X utf8 /path/to/claude-statusline/subagent_statusline.py"
  }
}
```

On Windows, write the path with forward slashes. Restart Claude Code afterwards; neither
setting is re-read while a session is open.

## The status line

| Segment | Example | Notes |
|---|---|---|
| Model and effort | `Opus 5 medium` | |
| Directory and git | `~/projects/webapp (main* ↑2)` | `*` for uncommitted or untracked changes, `↑` `↓` for commits ahead and behind |
| Session name | `gateway` | Shown once the session is named with `--name` or `/rename`, or gets a generated title |
| Context | `215k`, gauge | Tokens in use against the 400k and 500k ceilings |
| Rate limits | `5h`, gauge, `91% ⟳ 02:26  7d 41%` | The 5-hour reset time appears from 85% |
| Session | `$9.40  ◷ 2h 30m  +310 −88` | Cost, elapsed time, lines added and removed |

Numbers take your terminal theme's green, yellow and red. Gauges use a darker shade of
the same state so they stay quiet next to the numbers, and the model name is bold in
Claude orange. Rate limits turn yellow at 60% and red at 85%.

The context gauge widens with the terminal, from 10 cells up to 25 at 181 columns, always
a multiple of 5 so the 400k mark falls on a cell edge. The 5-hour limit gets its own gauge
from 175 columns. Widths depend only on the terminal size, so a gauge never changes length
between refreshes. The line stops 4 columns short of the terminal width, which Claude Code
keeps for its own margins.

The line never wraps. When it runs out of room, segments drop in this order: session
name, lines changed, elapsed time, cost, directory, rate limits. Model and context always
stay. The session name goes first because Claude Code already
shows a name set with `--name` or `/rename` on its prompt bar.

## Subagent rows

`subagent_statusline.py` replaces the default rows in the agent panel. Each row shows a
status mark (`▸` running, `·` pending, `✓` done, `✗` failed), the label, the model and
any explicit effort, the tokens used with the same context gauge as the main line, and
the description. The ceilings are capped by that agent's own window. Columns line up
across rows while the panel is wide enough.

The gauge appears once the agent's model is known, which needs Claude Code 2.1.205;
until then the row shows the token count alone.
Effort appears only when it was set explicitly, which needs 2.1.214; no effort means the
agent inherited the session's.

## Tests

```
python -X utf8 test_statusline.py
```

The suite uses only the standard library, so it runs anywhere the status line does. It
covers line width, colours, the symbols drawn, the subagent rows, and malformed input,
which may drop its own segment but never the line.

## Details

- Built for dark terminal themes.
- Some symbols, such as `◷` and `⟳`, are missing from many monospace fonts. Each one is
  followed by a space, so a wider fallback glyph cannot overlap the next character. Some
  fonts, such as Berkeley Mono, have no braille either; the terminal then draws the `⣿`
  track from a fallback font, still one cell wide.
- `git status` runs at most once every 5 seconds per session.
- `python statusline.py --version` prints the installed version. Bump `VERSION` in
  `statusline.py` whenever either script changes.

## License

MIT
