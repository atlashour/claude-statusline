#!/usr/bin/env python3
"""Test suite for statusline.py v5 and subagent_statusline.py.

Run on any machine that carries these scripts:

    python -X utf8 /path/to/claude-statusline/test_statusline.py

Stdlib only (unittest), no third-party dependencies, so it runs wherever
the status line itself runs. Every test drives the real render path with
a real payload; nothing is mocked except the terminal width, which is an
environment variable by design.
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
    """The version is reported by --version, never painted on the line.

    A version on the line reads as the model's version to anyone looking
    over your shoulder, so it stays out of the status bar entirely.
    """

    def test_version_is_absent_from_the_line(self):
        out = strip(render_at(200))
        self.assertNotIn(statusline.VERSION, out)
        self.assertNotIn(f"v{statusline.VERSION}", out)

    def test_version_is_absent_from_a_narrow_line_too(self):
        self.assertNotIn(statusline.VERSION, strip(render_at(60)))

    def test_version_flag_prints_the_full_version(self):
        r = subprocess.run(
            [sys.executable, "-X", "utf8",
             os.path.join(HERE, "statusline.py"), "--version"],
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
        # Assert on the name itself, not on the longest word of the whole
        # line: that made the test depend on the machine's temp dir length.
        name = "x" * 200
        out = strip(render_at(300, session_name=name))
        self.assertIn("x" * (statusline.SESSION_MAX - 1) + "…", out)
        self.assertNotIn("x" * statusline.SESSION_MAX, out)

    def test_absent_session_name_leaves_no_empty_separator(self):
        out = strip(render_at(200))
        self.assertNotIn("│  │", out)


class Language(unittest.TestCase):
    """Everything the line prints is English, including the saturation flag."""

    def test_window_suffix_is_english(self):
        out = strip(render_at(200))
        self.assertIn("of", out)
        self.assertNotIn(" de ", out)

    def test_saturation_flag_is_english(self):
        out = strip(render_at(200, context_window={
            "total_input_tokens": 300000,
            "context_window_size": 200000,
            "used_percentage": 150,
        }))
        self.assertIn("saturated", out)
        self.assertNotIn("saturado", out)


class WidthDiscipline(unittest.TestCase):
    def test_line_fits_a_narrow_terminal(self):
        self.assertLessEqual(statusline.visible_len(render_at(60)), 60)

    def test_line_fits_a_wide_terminal(self):
        self.assertLessEqual(statusline.visible_len(render_at(200)), 200)

    def test_line_fits_with_a_long_session_name(self):
        line = render_at(80, session_name="a very long session name indeed")
        self.assertLessEqual(statusline.visible_len(line), 80)


class FailureContainment(unittest.TestCase):
    """A malformed field may drop its own segment; it may never drop the line."""

    def test_empty_payload_still_renders(self):
        self.assertTrue(statusline.render({}).strip())

    def test_strings_where_numbers_belong_still_render(self):
        d = payload(context_window={"total_input_tokens": "many",
                                    "context_window_size": "big"},
                    cost={"total_cost_usd": None, "total_duration_ms": "soon"},
                    rate_limits={"five_hour": "nope"})
        self.assertTrue(statusline.render(d).strip())

    def test_wrong_types_for_whole_objects_still_render(self):
        d = payload(model="Opus", effort=[], workspace=None, session_name=42)
        self.assertTrue(statusline.render(d).strip())

    def test_non_finite_numbers_still_render_the_gauge(self):
        # Python's json accepts Infinity/NaN, and int() raises on both.
        d = json.loads('{"context_window": {"total_input_tokens": Infinity, '
                       '"used_percentage": NaN}}')
        out = strip(statusline.render(d))
        self.assertIn("ctx", out)
        self.assertNotIn("n/a", out)
        self.assertNotIn("nan", out)
        self.assertNotIn("inf", out)

    def test_malformed_five_hour_keeps_the_seven_day_limit(self):
        out = strip(render_at(200, rate_limits={
            "five_hour": "nope", "seven_day": {"used_percentage": 41.2}}))
        self.assertIn("7d 41%", out)

    def test_process_exits_zero_on_garbage_stdin(self):
        r = subprocess.run(
            [sys.executable, "-X", "utf8", os.path.join(HERE, "statusline.py")],
            input="not json at all", capture_output=True,
            encoding="utf-8", timeout=10,
        )
        self.assertEqual(r.returncode, 0)
        self.assertTrue(r.stdout.strip())


# --- subagent status line -------------------------------------------------

def tasks_payload(*tasks, columns=120):
    return {"session_id": "test-session-abc", "columns": columns,
            "tasks": list(tasks)}


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


class SubagentRows(unittest.TestCase):
    def test_one_json_line_per_task(self):
        rows = subagent_statusline.render_rows(
            tasks_payload(a_task(id="a"), a_task(id="b")))
        self.assertEqual([json.loads(r)["id"] for r in rows], ["a", "b"])

    def test_row_prefers_the_label_over_the_name(self):
        # label identifies the individual invocation; name is the agent type.
        rows = subagent_statusline.render_rows(tasks_payload(a_task()))
        self.assertIn("explore:parser", strip(json.loads(rows[0])["content"]))

    def test_row_falls_back_to_the_name_without_a_label(self):
        task = a_task()
        del task["label"]
        rows = subagent_statusline.render_rows(tasks_payload(task))
        self.assertIn("Explore", strip(json.loads(rows[0])["content"]))

    def test_row_shows_context_usage_when_the_model_is_resolved(self):
        rows = subagent_statusline.render_rows(tasks_payload(a_task()))
        self.assertIn("24%", strip(json.loads(rows[0])["content"]))

    def test_row_omits_context_usage_when_the_model_is_unresolved(self):
        # contextWindowSize is absent until the task's model resolves.
        task = a_task()
        del task["contextWindowSize"]
        rows = subagent_statusline.render_rows(tasks_payload(task))
        self.assertNotIn("%", strip(json.loads(rows[0])["content"]))

    def test_row_shows_effort_when_set(self):
        rows = subagent_statusline.render_rows(tasks_payload(a_task()))
        self.assertIn("high", strip(json.loads(rows[0])["content"]))

    def test_row_omits_effort_when_inherited(self):
        task = a_task()
        del task["effort"]
        rows = subagent_statusline.render_rows(tasks_payload(task))
        self.assertNotIn("high", strip(json.loads(rows[0])["content"]))

    def test_numeric_effort_budget_is_rendered(self):
        rows = subagent_statusline.render_rows(tasks_payload(a_task(effort=32000)))
        self.assertIn("32k", strip(json.loads(rows[0])["content"]))

    def test_row_fits_the_declared_column_budget(self):
        rows = subagent_statusline.render_rows(
            tasks_payload(a_task(description="d" * 300), columns=70))
        content = json.loads(rows[0])["content"]
        self.assertLessEqual(statusline.visible_len(content), 70)

    def test_no_tasks_produces_no_rows(self):
        self.assertEqual(subagent_statusline.render_rows(tasks_payload()), [])

    def test_task_without_an_id_is_skipped(self):
        # Per the docs, omitting a task's id keeps its default rendering.
        task = a_task()
        del task["id"]
        self.assertEqual(subagent_statusline.render_rows(tasks_payload(task)), [])

    def test_malformed_task_drops_only_that_row(self):
        rows = subagent_statusline.render_rows(
            tasks_payload(a_task(id="good"), "not a dict", a_task(id="also-good")))
        self.assertEqual([json.loads(r)["id"] for r in rows], ["good", "also-good"])

    def test_process_exits_zero_on_garbage_stdin(self):
        r = subprocess.run(
            [sys.executable, "-X", "utf8",
             os.path.join(HERE, "subagent_statusline.py")],
            input="not json at all", capture_output=True,
            encoding="utf-8", timeout=10,
        )
        self.assertEqual(r.returncode, 0)

    def test_version_flag_matches_the_main_status_line(self):
        r = subprocess.run(
            [sys.executable, "-X", "utf8",
             os.path.join(HERE, "subagent_statusline.py"), "--version"],
            capture_output=True, encoding="utf-8", timeout=10,
        )
        self.assertEqual(r.returncode, 0)
        self.assertIn(statusline.VERSION, r.stdout)


# --- encoding ---------------------------------------------------------------

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
        out = self.run_without_utf8_mode(
            "statusline.py", payload(session_name="Álvaro"))
        self.assertIn("Álvaro", out)

    def test_subagent_rows_decode_stdin_as_utf8(self):
        out = self.run_without_utf8_mode(
            "subagent_statusline.py", tasks_payload(a_task(label="Álvaro")))
        self.assertIn("Álvaro", out)


if __name__ == "__main__":
    unittest.main(verbosity=2)
