#!/usr/bin/env python3
"""Test suite for statusline.py and subagent_statusline.py.

    python -X utf8 test_statusline.py

Standard library only, so it runs wherever the status line runs. Every test drives the
real render path with a real payload; only the terminal width (an environment variable)
and, where stated, git state are patched.
"""
import json
import os
import subprocess
import sys
import tempfile
import unittest
from unittest import mock

HERE = os.path.dirname(os.path.abspath(__file__))
sys.path.insert(0, HERE)

import statusline                      # noqa: E402
import subagent_statusline             # noqa: E402

# Glyphs the scripts may draw beyond printable ASCII. Anything else risks a font fallback.
DRAWN_GLYPHS = set("█▌▓░⣿│·−…↑↓◷⟳▸✓✗")
# Glyphs missing from common terminal fonts: the fallback may be wider than a cell.
FALLBACK_GLYPHS = set("◷⟳")
GAUGE_GLYPHS = set("█▌▓░⣿")
GIT = {"branch": "main", "dirty": True, "ahead": 2, "behind": 1}


def payload(**over):
    """A complete, well-formed status line payload; `over` replaces keys."""
    d = {
        "model": {"id": "claude-opus-5", "display_name": "Opus"},
        "workspace": {"current_dir": tempfile.gettempdir()},
        "cwd": tempfile.gettempdir(),
        "session_id": "test-session-abc",
        "version": "2.1.247",
        "effort": {"level": "medium"},
        "context_window": {
            "total_input_tokens": 48000,
            "context_window_size": 200000,
            "used_percentage": 24,
        },
        "cost": {
            "total_cost_usd": 1.23,
            "total_duration_ms": 600000,
            "total_lines_added": 10,
            "total_lines_removed": 2,
        },
        "rate_limits": {
            "five_hour": {"used_percentage": 63.5, "resets_at": 1787000000},
            "seven_day": {"used_percentage": 41.2, "resets_at": 1787500000},
        },
    }
    d.update(over)
    return d


def render_at(cols, **over):
    """render() the payload at a fixed terminal width."""
    with mock.patch.dict(os.environ, {"COLUMNS": str(cols)}):
        return statusline.render(payload(**over))


def context(tokens, window=1_000_000):
    return {"total_input_tokens": tokens, "context_window_size": window}


def context_gauge(text):
    """The first run of gauge glyphs: the context gauge, which precedes the 5h gauge."""
    visible = strip(text)
    start = next(i for i, ch in enumerate(visible) if ch in GAUGE_GLYPHS)
    end = start
    while end < len(visible) and visible[end] in GAUGE_GLYPHS:
        end += 1
    return visible[start:end]


def gauge_start(text):
    return next(i for i, ch in enumerate(strip(text)) if ch in GAUGE_GLYPHS)


def strip(s):
    """Drop ANSI escapes so tests assert on visible text."""
    out, i, n = [], 0, len(s)
    while i < n:
        if s[i] == "\033" and i + 1 < n and s[i + 1] == "[":
            j = i + 2
            while j < n and not ("@" <= s[j] <= "~"):
                j += 1
            i = j + 1
        else:
            out.append(s[i])
            i += 1
    return "".join(out)


class Version(unittest.TestCase):
    """The version is reported by --version, never painted on the line."""

    def test_version_is_absent_from_the_line(self):
        out = strip(render_at(200))
        self.assertNotIn(statusline.VERSION, out)

    def test_version_is_absent_from_a_narrow_line_too(self):
        self.assertNotIn(statusline.VERSION, strip(render_at(60)))

    def test_version_flag_prints_the_full_version(self):
        r = subprocess.run(
            [sys.executable, "-X", "utf8", os.path.join(HERE, "statusline.py"), "--version"],
            capture_output=True, encoding="utf-8", timeout=10,
        )
        self.assertEqual(r.returncode, 0)
        self.assertIn(statusline.VERSION, r.stdout)


class SessionName(unittest.TestCase):
    def test_session_name_is_displayed_when_set(self):
        self.assertIn("gateway", strip(render_at(200, session_name="gateway")))

    def test_long_session_name_is_truncated_with_an_ellipsis(self):
        name = "Investigate the intermittent failure in the parser test suite"
        out = strip(render_at(200, session_name=name))
        self.assertNotIn(name, out)
        self.assertIn("…", out)

    def test_truncated_session_name_respects_the_cell_budget(self):
        out = strip(render_at(300, session_name="x" * 200))
        self.assertIn("x" * (statusline.SESSION_MAX - 1) + "…", out)
        self.assertNotIn("x" * statusline.SESSION_MAX, out)

    def test_absent_session_name_leaves_no_empty_separator(self):
        self.assertNotIn("│  │", strip(render_at(200)))

    def test_control_and_format_characters_are_neutralised(self):
        out = render_at(200, session_name="a\tb\nc‮d e f")
        for ch in ("\t", "\n", "‮", " ", " "):
            self.assertNotIn(ch, out)


class ContextGauge(unittest.TestCase):
    def test_gauge_has_the_width_of_its_tier(self):
        for cols in (80, 150, 170, 186, 240):
            ctx_w, _ = statusline.gauge_widths(cols)
            self.assertEqual(len(context_gauge(render_at(cols, context_window=context(125_000)))), ctx_w, cols)

    def test_context_widths_put_the_ordinary_ceiling_on_a_cell_edge(self):
        for _, ctx_w, _ in statusline.GAUGE_TIERS:
            self.assertEqual(ctx_w % 5, 0, ctx_w)

    def test_gauge_width_ignores_the_data(self):
        # Widths come from COLUMNS alone, so a refresh can never make the gauge jump.
        states = [dict(context_window=context(t), cost={"total_cost_usd": c},
                       rate_limits={"five_hour": {"used_percentage": p, "resets_at": 1787000000}})
                  for t in (0, 150_000, 450_000, 900_000) for c in (0, 999.99) for p in (5, 85, 100)]
        widths = set()
        with mock.patch.dict(os.environ, {"COLUMNS": "186"}):
            for s in states:
                widths.add(len(context_gauge(statusline.render(payload(**s)))))
        self.assertEqual(widths, {statusline.gauge_widths(186)[0]})

    def test_gauge_starts_at_the_same_column_as_the_count_grows(self):
        starts = {gauge_start(render_at(200, context_window=context(t))) for t in (0, 9_000, 312_000, 1_250_000)}
        self.assertEqual(len(starts), 1)

    def test_gauge_tiers_never_shrink_when_widening(self):
        widths = [statusline.gauge_widths(c) for c in range(-5, 400)]
        for field in (0, 1):
            series = [w[field] for w in widths]
            self.assertEqual(series, sorted(series))

    def test_colour_changes_exactly_at_the_two_ceilings(self):
        for tokens, colour in ((399_999, statusline.GREEN), (400_000, statusline.AMBER),
                               (499_999, statusline.AMBER), (500_000, statusline.RED)):
            out = render_at(200, context_window=context(tokens))
            self.assertIn(f"\033[{colour}m{statusline.tokens(tokens):>4}", out, tokens)

    def test_ceilings_are_clamped_to_the_window(self):
        # A 200k window: absolute 160k (0.8 of the window), ordinary 128k.
        for tokens, colour in ((127_999, statusline.GREEN), (128_000, statusline.AMBER),
                               (159_999, statusline.AMBER), (160_000, statusline.RED)):
            out = render_at(200, context_window=context(tokens, 200_000))
            self.assertIn(f"\033[{colour}m{statusline.tokens(tokens):>4}", out, tokens)

    def test_unknown_window_uses_the_absolute_ceiling(self):
        out = render_at(200, context_window={"total_input_tokens": 450_000})
        self.assertIn(f"\033[{statusline.AMBER}m450k", out)

    def test_bar_is_full_only_at_the_absolute_ceiling(self):
        self.assertIn("⣿", context_gauge(render_at(200, context_window=context(499_999))))
        for tokens in (500_000, 2_000_000):
            gauge = context_gauge(render_at(200, context_window=context(tokens)))
            self.assertEqual(set(gauge), {"█", "▓"}, tokens)

    def test_last_mile_has_its_own_texture(self):
        # 25 cells: 20 before the ordinary ceiling, 5 in the last mile, each 20k wide.
        def at(tokens):
            return statusline.context_gauge(tokens, 500_000, 25, statusline.GREEN)
        self.assertEqual(strip(at(0)), "░" * 20 + "⣿" * 5)
        self.assertEqual(strip(at(390_000)), "█" * 19 + "▌" + "⣿" * 5)
        self.assertEqual(strip(at(400_000)), "█" * 20 + "⣿" * 5)
        self.assertEqual(strip(at(450_000)), "█" * 20 + "▓" * 2 + "⣿" * 3)
        self.assertEqual(strip(at(499_999)), "█" * 20 + "▓" * 4 + "⣿")
        self.assertEqual(strip(at(500_000)), "█" * 20 + "▓" * 5)

    def test_count_is_floored_so_no_ceiling_shows_early(self):
        self.assertEqual(statusline.tokens(399_600), "399k")
        self.assertEqual(statusline.tokens(999), "999")
        self.assertEqual(statusline.tokens(999_999), "999k")
        self.assertEqual(statusline.tokens(1_250_000), "1.2M")
        self.assertEqual(statusline.tokens(12_345_678), "12M")

    def test_segment_is_count_and_gauge_only(self):
        out = strip(render_at(240, context_window=context(450_000)))
        for word in ("ctx", "%", " of ", "/500k", "/400k", "saturated", "◬"):
            self.assertNotIn(word, out.split("│ 5h", 1)[0])

    def test_bars_use_the_darker_shade_and_the_count_the_theme_colour(self):
        out = render_at(240, context_window=context(450_000),
                        rate_limits={"five_hour": {"used_percentage": 10}})
        self.assertIn(f"\033[{statusline.AMBER}m450k", out)
        self.assertIn(f"\033[{statusline.BAR[statusline.AMBER]}m█", out)
        self.assertIn(f"\033[{statusline.BAR[statusline.GREEN]}m█", out)     # the 5h gauge

    def test_model_name_is_bold_claude_orange(self):
        self.assertIn(f"\033[1;{statusline.CLAUDE}mOpus\033[0m", render_at(200))

    def test_state_colours_are_never_bold(self):
        # Many terminals render bold colours in their bright variant: one state, one tone.
        out = render_at(240, context_window=context(600_000, 1_000_000),
                        rate_limits={"five_hour": {"used_percentage": 95, "resets_at": 1787000000}})
        for colour in (statusline.GREEN, statusline.AMBER, statusline.RED, *statusline.BAR.values()):
            self.assertNotIn(f"\033[1;{colour}m", out)

    def test_huge_token_counts_are_displayed_in_millions(self):
        self.assertEqual(statusline.human(999_500), "1M")
        self.assertEqual(statusline.human(1_250_000), "1.25M")
        self.assertEqual(statusline.human(250_000), "250k")

    def test_tiny_windows_start_green_and_empty(self):
        for window in (1, 2, 5):
            part = statusline.context_part(0, window, 10)
            self.assertIn(f"\033[{statusline.GREEN}m", part, window)
            self.assertEqual(context_gauge(part), "░" * 8 + "⣿" * 2, window)

    def test_narrow_gauges_keep_their_width(self):
        for width in (1, 2, 3, 4):
            for used in (0, 399_999, 400_000, 450_000, 499_999, 500_000):
                gauge = strip(statusline.context_gauge(used, 500_000, width, statusline.GREEN))
                self.assertEqual(len(gauge), width, (width, used))

    def test_negative_tokens_draw_an_empty_gauge(self):
        out = render_at(100, context_window=context(-100_000))
        self.assertEqual(context_gauge(out), "░" * 8 + "⣿" * 2)
        self.assertIn("   0 ", strip(out))

    def test_negative_columns_still_render(self):
        with mock.patch.dict(os.environ, {"COLUMNS": "-5"}):
            self.assertNotIn("n/a", strip(statusline.render(payload())))


class RateLimits(unittest.TestCase):
    def test_reset_time_appears_from_the_hot_threshold(self):
        def limits(p):
            return {"five_hour": {"used_percentage": p, "resets_at": 1787000000}}
        self.assertNotIn("⟳", strip(render_at(120, rate_limits=limits(84.4))))
        self.assertIn("⟳ ", strip(render_at(120, rate_limits=limits(84.6))))

    def test_only_the_five_hour_limit_gets_a_gauge(self):
        out = strip(render_at(240))
        five, seven = out.split("5h", 1)[1].split("7d", 1)
        self.assertIn("█", five)
        self.assertNotIn("█", seven.split("│")[0])
        self.assertNotIn("░", seven.split("│")[0])

    def test_malformed_five_hour_keeps_the_seven_day_limit(self):
        out = strip(render_at(200, rate_limits={
            "five_hour": "nope", "seven_day": {"used_percentage": 41.2}}))
        self.assertIn("7d 41%", out)


class Colour(unittest.TestCase):
    def test_red_is_reserved_for_alarms(self):
        # A healthy session: lines removed must not look like an alarm.
        out = render_at(240, context_window=context(20_000),
                        rate_limits={"five_hour": {"used_percentage": 10}},
                        cost={"total_lines_added": 5, "total_lines_removed": 900})
        self.assertNotIn(f"{statusline.RED}m", out)

    def test_no_sgr_dim_is_used(self):
        # Claude Code dims the whole line already; SGR 2 would carry no hierarchy.
        self.assertNotIn("\033[2m", render_at(240))


class Layout(unittest.TestCase):
    def test_line_fits_every_width(self):
        long = dict(session_name="a very long session name indeed",
                    context_window=context(262_000), cost={"total_cost_usd": 123.45,
                    "total_duration_ms": 36_000_000, "total_lines_added": 4000, "total_lines_removed": 900})
        for cols in range(70, 260):
            width = statusline.visible_len(render_at(cols, **long))
            self.assertLessEqual(width, cols - statusline.RESERVE, cols)

    def test_directory_comes_before_the_session_name(self):
        with mock.patch.object(statusline, "git_info", return_value=GIT):
            out = strip(render_at(300, session_name="gateway"))
        self.assertLess(out.index("(main"), out.index("gateway"))

    def test_session_name_is_the_first_segment_to_drop(self):
        # One fixed gauge tier, so only the segment set can change with the width.
        full = dict(session_name="gateway", context_window=context(215_000))
        with mock.patch.object(statusline, "GAUGE_TIERS", ((0, 10, 0),)):
            fits = statusline.visible_len(render_at(400, **full)) + statusline.RESERVE
            self.assertIn("gateway", strip(render_at(fits, **full)))
            out = strip(render_at(fits - 1, **full))
        self.assertNotIn("gateway", out)
        self.assertIn("+10 −2", out)

    def test_cost_duration_and_lines_read_as_one_group(self):
        out = strip(render_at(300))
        self.assertIn("$1.23  ◷ 10m  +10 −2", out)

    def test_git_state_is_shown_next_to_the_directory(self):
        with mock.patch.object(statusline, "git_info", return_value=GIT):
            out = strip(render_at(240))
        self.assertIn("(main* ↑2↓1)", out)


class Glyphs(unittest.TestCase):
    """Only glyphs from a known list: a missing one falls back to another font and breaks alignment."""

    def assert_known_glyphs(self, text):
        visible = strip(text)
        unknown = {ch for ch in visible if ord(ch) > 126 and ch not in DRAWN_GLYPHS}
        self.assertEqual(unknown, set())
        for i, ch in enumerate(visible):
            if ch in FALLBACK_GLYPHS:
                self.assertEqual(visible[i + 1:i + 2], " ", f"{ch} must be followed by a space")

    def test_status_line_glyphs(self):
        with mock.patch.object(statusline, "git_info", return_value=GIT):
            for cols in (80, 186, 240):
                self.assert_known_glyphs(render_at(cols, context_window=context(262_000),
                                                   session_name="x" * 40,
                                                   rate_limits={"five_hour": {"used_percentage": 95,
                                                                              "resets_at": 1787000000}}))

    def test_subagent_row_glyphs(self):
        rows = subagent_statusline.render_rows(tasks_payload(
            a_task(id="a"), a_task(id="b", status="completed"), a_task(id="c", status="failed", tokenCount=250_000),
            a_task(id="d", status="pending", label="x" * 40)))
        for row in rows:
            self.assert_known_glyphs(json.loads(row)["content"])


class FailureContainment(unittest.TestCase):
    """A malformed field may drop its own segment; it may never drop the line."""

    def test_empty_payload_still_renders(self):
        self.assertTrue(statusline.render({}).strip())

    def test_strings_where_numbers_belong_still_render(self):
        d = payload(context_window={"total_input_tokens": "many", "context_window_size": "big"},
                    cost={"total_cost_usd": None, "total_duration_ms": "soon"},
                    rate_limits={"five_hour": "nope"})
        self.assertTrue(statusline.render(d).strip())

    def test_wrong_types_for_whole_objects_still_render(self):
        d = payload(model="Opus", effort=[], workspace=None, session_name=42)
        self.assertTrue(statusline.render(d).strip())

    def test_non_finite_numbers_still_render_the_gauge(self):
        d = json.loads('{"context_window": {"total_input_tokens": Infinity, "used_percentage": NaN}}')
        out = strip(statusline.render(d))
        self.assertTrue(context_gauge(out))
        for word in ("n/a", "nan", "inf"):
            self.assertNotIn(word, out)

    def test_integers_beyond_float_range_still_render(self):
        big = 10 ** 400                        # float() raises OverflowError, not ValueError
        out = strip(statusline.render({"context_window": {"total_input_tokens": big},
                                       "cost": {"total_cost_usd": big, "total_duration_ms": 5000}}))
        self.assertTrue(context_gauge(out))
        self.assertIn("◷ 5s", out)

    def test_process_exits_zero_on_garbage_stdin(self):
        r = subprocess.run(
            [sys.executable, "-X", "utf8", os.path.join(HERE, "statusline.py")],
            input="not json at all", capture_output=True, encoding="utf-8", timeout=10,
        )
        self.assertEqual(r.returncode, 0)
        self.assertTrue(r.stdout.strip())


# --- subagent status line ---------------------------------------------------------

def tasks_payload(*tasks, columns=120):
    return {"session_id": "test-session-abc", "columns": columns, "tasks": list(tasks)}


def a_task(**over):
    t = {
        "id": "task-1",
        "name": "Explore",
        "type": "Explore",
        "status": "running",
        "description": "locate the tokenizer entry points",
        "label": "explore:parser",
        "startTime": 1787000000000,
        "model": "claude-opus-5",
        "effort": "high",
        "contextWindowSize": 200000,
        "tokenCount": 48000,
    }
    t.update(over)
    return t


def contents(d):
    return [strip(json.loads(r)["content"]) for r in subagent_statusline.render_rows(d)]


class SubagentRows(unittest.TestCase):
    def test_one_json_line_per_task(self):
        rows = subagent_statusline.render_rows(tasks_payload(a_task(id="a"), a_task(id="b")))
        self.assertEqual([json.loads(r)["id"] for r in rows], ["a", "b"])

    def test_row_prefers_the_label_over_the_name(self):
        self.assertIn("explore:parser", contents(tasks_payload(a_task()))[0])

    def test_row_falls_back_to_the_name_without_a_label(self):
        task = a_task()
        del task["label"]
        self.assertIn("Explore", contents(tasks_payload(task))[0])

    def test_row_shows_count_and_gauge_when_the_model_is_resolved(self):
        row = contents(tasks_payload(a_task()))[0]
        self.assertIn(" 48k ", row)
        self.assertEqual(len(context_gauge(row)), subagent_statusline.BAR_W)
        self.assertNotIn("%", row)

    def test_row_shows_only_the_count_when_the_model_is_unresolved(self):
        task = a_task()
        del task["contextWindowSize"]
        row = contents(tasks_payload(task))[0]
        self.assertIn("48k", row)
        self.assertFalse(GAUGE_GLYPHS & set(row))

    def test_row_gauge_follows_the_task_ceilings(self):
        # 200k window: ordinary 128k, absolute 160k, as on the main line.
        for tokens, colour in ((127_999, statusline.GREEN), (128_000, statusline.AMBER),
                               (160_000, statusline.RED)):
            row = json.loads(subagent_statusline.render_rows(
                tasks_payload(a_task(tokenCount=tokens)))[0])["content"]
            self.assertIn(f"\033[{colour}m{statusline.tokens(tokens):>4}", row, tokens)
            self.assertIn(f"\033[{statusline.BAR[colour]}m", row, tokens)

    def test_row_shows_effort_when_set(self):
        self.assertIn("high", contents(tasks_payload(a_task()))[0])

    def test_row_omits_effort_when_inherited(self):
        task = a_task()
        del task["effort"]
        self.assertNotIn("high", contents(tasks_payload(task))[0])

    def test_numeric_effort_budget_is_rendered(self):
        self.assertIn("32k", contents(tasks_payload(a_task(effort=32000)))[0])

    def test_boolean_or_unrepresentable_effort_is_ignored(self):
        rows = contents(tasks_payload(a_task(id="a", effort=True), a_task(id="b", effort=10 ** 400)))
        self.assertEqual(len(rows), 2)
        self.assertNotIn("True", rows[0])

    def test_columns_align_across_rows(self):
        rows = contents(tasks_payload(a_task(id="a", label="x"), a_task(id="b", label="a-longer-label",
                                                                          model="claude-haiku-4-5-20251001")))
        self.assertEqual(gauge_start(rows[0]), gauge_start(rows[1]))

    def test_a_hostile_sibling_shifts_the_columns_by_a_bounded_amount(self):
        # Sibling fields are elided to fixed budgets, so no task can push the others off screen.
        alone = contents(tasks_payload(a_task(id="a")))
        crowded = contents(tasks_payload(a_task(id="a"), {"id": "x", "label": "z" * 90, "tokenCount": 1e300,
                                                          "contextWindowSize": 1, "effort": "y" * 90}))
        shift = gauge_start(crowded[0]) - gauge_start(alone[0])
        self.assertLessEqual(shift, (subagent_statusline.NAME_MAX - len("explore:parser"))
                             + (subagent_statusline.META_MAX - len("opus-5 high")))

    def test_narrow_panel_gives_up_alignment_to_keep_the_description(self):
        tasks = (a_task(id="a"), a_task(id="b", label="a-much-longer-label"))
        wide, narrow = contents(tasks_payload(*tasks, columns=120)), contents(tasks_payload(*tasks, columns=70))
        self.assertEqual(gauge_start(wide[0]), gauge_start(wide[1]))
        self.assertNotEqual(gauge_start(narrow[0]), gauge_start(narrow[1]))
        self.assertIn("locate the", narrow[0])

    def test_packed_rows_drop_the_count_padding(self):
        rows = contents(tasks_payload(a_task(id="a", tokenCount=5_000),
                                      a_task(id="b", label="a-much-longer-label"), columns=70))
        self.assertIn("opus-5 high  5k ", rows[0])

    def test_rows_still_align_without_the_main_script(self):
        # A failed import must degrade to plain text, never to ragged columns.
        code = ("import sys, json; sys.modules['statusline'] = None; sys.path.insert(0, sys.argv[1]);"
                "import subagent_statusline as s;"
                "t = lambda i, **o: dict({'id': i, 'label': i, 'model': 'claude-opus-5', 'description': 'desc'}, **o);"
                "rows = s.render_rows({'columns': 120, 'tasks': [t('a', contextWindowSize=1e6, tokenCount=5000),"
                "t('b', contextWindowSize=1e6, tokenCount=1250000), t('c', tokenCount=12000)]});"
                "print(json.dumps([json.loads(r)['content'].index('desc') for r in rows]))")
        r = subprocess.run([sys.executable, "-X", "utf8", "-c", code, HERE],
                           capture_output=True, encoding="utf-8", timeout=10)
        self.assertEqual(r.returncode, 0, r.stderr)
        self.assertEqual(len(set(json.loads(r.stdout))), 1, r.stdout)

    def test_task_past_its_ceiling_has_a_full_red_bar(self):
        row = json.loads(subagent_statusline.render_rows(
            tasks_payload(a_task(tokenCount=250_000)))[0])["content"]
        self.assertIn(f"\033[{statusline.RED}m250k", row)
        self.assertEqual(set(context_gauge(row)), {"█", "▓"})

    def test_failed_task_glyph_is_red(self):
        row = json.loads(subagent_statusline.render_rows(tasks_payload(a_task(status="failed")))[0])["content"]
        self.assertIn(f"\033[{statusline.RED}m✗", row)

    def test_row_fits_the_declared_column_budget(self):
        rows = subagent_statusline.render_rows(tasks_payload(a_task(description="d" * 300), columns=70))
        self.assertLessEqual(statusline.visible_len(json.loads(rows[0])["content"]), 70)

    def test_no_tasks_produces_no_rows(self):
        self.assertEqual(subagent_statusline.render_rows(tasks_payload()), [])

    def test_task_without_an_id_is_skipped(self):
        task = a_task()
        del task["id"]
        self.assertEqual(subagent_statusline.render_rows(tasks_payload(task)), [])

    def test_malformed_task_drops_only_that_row(self):
        rows = subagent_statusline.render_rows(
            tasks_payload(a_task(id="good"), "not a dict", a_task(id="also-good")))
        self.assertEqual([json.loads(r)["id"] for r in rows], ["good", "also-good"])

    def test_process_exits_zero_on_garbage_stdin(self):
        r = subprocess.run(
            [sys.executable, "-X", "utf8", os.path.join(HERE, "subagent_statusline.py")],
            input="not json at all", capture_output=True, encoding="utf-8", timeout=10,
        )
        self.assertEqual(r.returncode, 0)

    def test_version_flag_matches_the_main_status_line(self):
        r = subprocess.run(
            [sys.executable, "-X", "utf8", os.path.join(HERE, "subagent_statusline.py"), "--version"],
            capture_output=True, encoding="utf-8", timeout=10,
        )
        self.assertEqual(r.returncode, 0)
        self.assertIn(statusline.VERSION, r.stdout)


# --- encoding -----------------------------------------------------------------------

class Encoding(unittest.TestCase):
    """Both scripts decode stdin as UTF-8 even when run without `-X utf8`."""

    def run_without_utf8_mode(self, script, data):
        env = {k: v for k, v in os.environ.items() if k != "PYTHONIOENCODING"}
        env["PYTHONUTF8"] = "0"                # the legacy locale codec (cp1252)
        r = subprocess.run(
            [sys.executable, os.path.join(HERE, script)],
            input=json.dumps(data, ensure_ascii=False).encode("utf-8"),
            capture_output=True, env=env, timeout=10,
        )
        self.assertEqual(r.returncode, 0)
        return r.stdout.decode("utf-8")

    def test_status_line_decodes_stdin_as_utf8(self):
        self.assertIn("Álvaro", self.run_without_utf8_mode("statusline.py", payload(session_name="Álvaro")))

    def test_subagent_rows_decode_stdin_as_utf8(self):
        out = self.run_without_utf8_mode("subagent_statusline.py", tasks_payload(a_task(label="Álvaro")))
        self.assertIn("Álvaro", out)


if __name__ == "__main__":
    unittest.main(verbosity=2)
