# claude-statusline

A status line for Claude Code that answers one question: how close am I to the
point where the model starts losing the thread.

```
Opus medium │ gateway │ ~/projects/webapp (main*↑2) │ ctx 215k/250k ██████████░░ 86% · 21% of 1M │ 5h 91% ⟳22:53 7d 41% │ $9.40 │ ◷ 2h 30m │ +310 −88
```

## Why the gauge is not the context window

Every status line I tried plots usage against the model's context window, so a
1M window sits at a comfortable 20% while the answers have already gone vague.
Context rot is absolute, not proportional. Quality degrades somewhere north of
200k tokens whether the window is 200k or a million.

So the bar plots usage against a comfort budget instead: 250k tokens, clamped to
80% of the real window so it can never promise headroom the window does not
have. On a 200k window it turns red at 160k, before compaction is forced on you.
On a 1M window the 250k ceiling governs. The window percentage is still there,
dimmed, at the end of the gauge.

## Install

Requires Python 3.7 or later and Claude Code 2.1.153 or later. No dependencies.

Clone it wherever you keep things, then point `~/.claude/settings.json` at it:

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

On Windows use forward slashes in the path. Backslashes get eaten as escapes
before the script ever runs.

Restart Claude Code. Neither block is re-read while a session is open.

## What the line shows

Left to right: model and effort level, session name, directory with git branch
and dirty or ahead/behind markers, the context gauge, subscription rate limits
for the 5 hour and 7 day windows, session cost, elapsed time, and lines changed.

The line never wraps. When the terminal is too narrow, segments drop from the
least important: lines changed, then duration, then cost, then directory, then
rate limits. Model and gauge never drop. As a last resort the gauge sheds its
window suffix.

Session name is the handle other sessions address you by with `SendMessage`. It
only appears if you named the session with `--name` or `/rename`, or once an
AI-generated title exists. The default display name, the `my-app-3f` kind, does
not populate that field, so name your sessions if you want to see it.

## Subagent rows

`subagent_statusline.py` replaces the default `name · description · token count`
rows in the agent panel:

```
▸ explore:parser  opus-5 high    █░░░░░ 24%   48k   locate the tokenizer entry points
▸ review:api      haiku-4-5      █████░ 90%  181k   review the branch diff
✓ Plan            sonnet-5 32k   ░░░░░░  6%   12k   design the migration
```

The gauge here is against the task's own context window, which is the number
that matters for an agent about to run out of room. It only appears once the
task's model resolves, because a percentage against a guessed window is a
confident lie. Effort only appears when the subagent was given one explicitly;
absent means it inherited the session's.

Model and context need Claude Code 2.1.205, effort needs 2.1.214.

## Version

```
python statusline.py --version
```

Which build a machine is running, for when the same file is deployed to several
of them. It is not drawn on the line on purpose: a version sitting a few
characters from the model name gets read as the model's version.

Bump `VERSION` in `statusline.py` when you change either file. The subagent
script imports it, so they stay in step.

## Tests

```
python -X utf8 test_statusline.py
```

Standard library only, so it runs anywhere the status line does. Covers the
width discipline, the eliding, the subagent rows, and the failure containment:
a malformed field drops its own segment and never the line. Run it after
copying to a new machine. If it passes, the line works.

## Failure behaviour

Every optional segment is built behind its own guard. Bad data makes that
segment disappear, not the line. Object-typed fields are read defensively, so a
field arriving as a string degrades to nothing instead of raising. A failure in
the core path prints `ctx: n/a`. Stdin and stdout are forced to UTF-8 whether or
not you pass `-X utf8`, since Windows defaults to cp1252, which would garble
non-ASCII names on the way in and choke on the bar glyphs on the way out.

Git state is cached for 5 seconds per session, keyed by session id and
invalidated when the directory changes, so `git status` does not run on every
render. Dirty and ahead/behind can lag by that much. That is the trade.

## License

MIT
