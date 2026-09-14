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
DRAWN_GLYPHS = set("█░│·−…↑↓◬◷⟳▸✓✗")
# Glyphs missing from common terminal fonts: the fallback may be wider than a cell.
FALLBACK_GLYPHS = set("◬◷⟳")
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
        for cols in (80, 150, 186, 240):
            ctx_w, _ = statusline.gauge_widths(cols)
            out = strip(render_at(cols, context_window=context(125_000)))
            drawn = out.split("/250k ", 1)[1].split(" ", 1)[0]
            half = round(ctx_w / 2)
            self.assertEqual(drawn, "█" * half + "░" * (ctx_w - half), cols)

    def test_gauge_width_ignores_the_data(self):
        # Widths come from COLUMNS alone, so a refresh can never make the gauge jump.
        states = [dict(context_window=context(t), cost={"total_cost_usd": c},
                       rate_limits={"five_hour": {"used_percentage": p, "resets_at": 1787000000}})
                  for t in (0, 150_000, 400_000) for c in (0, 999.99) for p in (5, 85, 100)]
        widths = set()
        with mock.patch.dict(os.environ, {"COLUMNS": "186"}):
            for s in states:
                ctx_gauge = strip(statusline.render(payload(**s))).split("ctx ", 1)[1].split("%", 1)[0]
                widths.add(ctx_gauge.count("█") + ctx_gauge.count("░"))
        self.assertEqual(widths, {statusline.gauge_widths(186)[0]})

    def test_gauge_tiers_never_shrink_when_widening(self):
        widths = [statusline.gauge_widths(c) for c in range(-5, 400)]
        for field in (0, 1):
            series = [w[field] for w in widths]
            self.assertEqual(series, sorted(series))

    def test_budget_is_clamped_to_the_window(self):
        self.assertIn("/160k", strip(render_at(200, context_window=context(10_000, 200_000))))

    def test_saturation_is_flagged_in_red(self):
        out = render_at(200, context_window=context(300_000, 200_000))
        self.assertIn("saturated", strip(out))
        self.assertIn(f"\033[{statusline.RED}m◬ saturated", out)

    def test_state_colours_are_never_bold(self):
        # Many terminals render bold colours in their bright variant: one state, one tone.
        out = render_at(240, context_window=context(300_000, 200_000),
                        rate_limits={"five_hour": {"used_percentage": 95, "resets_at": 1787000000}})
        for colour in (statusline.GREEN, statusline.AMBER, statusline.RED):
            self.assertNotIn(f"\033[1;{colour}m", out)

    def test_colour_follows_the_displayed_percentage(self):
        # 149_000 / 250k is 59.6%, displayed as 60%: it must already be amber.
        out = render_at(200, context_window=context(149_000))
        self.assertIn(f"\033[{statusline.AMBER}m60%", out)

    def test_huge_token_counts_are_displayed_in_millions(self):
        self.assertEqual(statusline.human(999_500), "1M")
        self.assertEqual(statusline.human(1_250_000), "1.25M")
        self.assertEqual(statusline.human(250_000), "250k")

    def test_negative_tokens_draw_an_empty_gauge(self):
        out = strip(render_at(100, context_window=context(-100_000)))
        ctx_segment = out.split("ctx ", 1)[1].split("│", 1)[0]
        self.assertTrue(ctx_segment.startswith("0/250k " + "░" * 12 + " 0%"), ctx_segment)

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
            self.assertLessEqual(statusline.visible_len(render_at(cols, **long)), cols, cols)

    def test_window_percentage_drops_before_the_limits(self):
        for cols in range(40, 200):
            out = strip(render_at(cols, context_window=context(215_000)))
            if "5h" not in out:
                self.assertNotIn(" of ", out, cols)

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
        self.assertIn("ctx", out)
        for word in ("n/a", "nan", "inf"):
            self.assertNotIn(word, out)

    def test_integers_beyond_float_range_still_render(self):
        big = 10 ** 400                        # float() raises OverflowError, not ValueError
        out = strip(statusline.render({"context_window": {"total_input_tokens": big},
                                       "cost": {"total_cost_usd": big, "total_duration_ms": 5000}}))
        self.assertIn("ctx", out)
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

    def test_row_shows_context_usage_when_the_model_is_resolved(self):
        self.assertIn("24%", contents(tasks_payload(a_task()))[0])

    def test_row_omits_context_usage_when_the_model_is_unresolved(self):
        task = a_task()
        del task["contextWindowSize"]
        self.assertNotIn("%", contents(tasks_payload(task))[0])

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
        self.assertEqual(rows[0].index("%"), rows[1].index("%"))

    def test_a_hostile_sibling_shifts_the_columns_by_a_bounded_amount(self):
        # Sibling fields are elided to fixed budgets, so no task can push the others off screen.
        alone = contents(tasks_payload(a_task(id="a")))
        crowded = contents(tasks_payload(a_task(id="a"), {"id": "x", "label": "z" * 90, "tokenCount": 1e300,
                                                          "contextWindowSize": 1, "effort": "y" * 90}))
        shift = crowded[0].index("%") - alone[0].index("%")
        self.assertLessEqual(shift, (subagent_statusline.NAME_MAX - len("explore:parser"))
                             + (subagent_statusline.META_MAX - len("opus-5 high")))

    def test_narrow_panel_gives_up_alignment_to_keep_the_description(self):
        tasks = (a_task(id="a"), a_task(id="b", label="a-much-longer-label"))
        wide, narrow = contents(tasks_payload(*tasks, columns=120)), contents(tasks_payload(*tasks, columns=70))
        self.assertEqual(wide[0].index("%"), wide[1].index("%"))
        self.assertNotEqual(narrow[0].index("%"), narrow[1].index("%"))
        self.assertIn("locate the", narrow[0])

    def test_saturated_task_is_flagged(self):
        self.assertIn("◬", contents(tasks_payload(a_task(tokenCount=250_000)))[0])

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
