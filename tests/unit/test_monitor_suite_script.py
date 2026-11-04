#!/usr/bin/env python3
"""Unit tests for the pure-logic parts of `scripts/ci/monitor_suite.py`.

`scripts/ci/monitor_suite.py` is a standalone script (not part of the `tactus`
package), so it is loaded here via `importlib` rather than a normal import.
"""

import importlib.util
import sys
from pathlib import Path

_SCRIPT_PATH = Path(__file__).resolve().parents[2] / "scripts" / "ci" / "monitor_suite.py"
_SPEC = importlib.util.spec_from_file_location("monitor_suite_script", _SCRIPT_PATH)
monitor_suite = importlib.util.module_from_spec(_SPEC)
sys.modules[_SPEC.name] = monitor_suite
_SPEC.loader.exec_module(monitor_suite)


class _FakeServer:
    """Fake `EcflowServer` returning canned states/recording suspend() calls."""

    def __init__(self, states_by_path):
        self.states_by_path = dict(states_by_path)
        self.suspended = []

    def get_node_state(self, path):
        return self.states_by_path.get(path)

    def suspend(self, path):
        self.suspended.append(path)


class TestPollTargets:
    """Unit tests for `poll_targets()`."""

    def test_all_terminal_on_first_poll(self):
        targets = [
            monitor_suite.MonitorTarget("a", "/a/CIMonitor", "unused"),
            monitor_suite.MonitorTarget("b", "/b/CIMonitor", "unused"),
        ]
        server = _FakeServer({"/a/CIMonitor": "complete", "/b/CIMonitor": "aborted"})

        states = monitor_suite.poll_targets(
            server, targets, poll_interval_seconds=0, deadline=0
        )

        assert states == {"a": "complete", "b": "aborted"}

    def test_times_out_when_never_terminal(self):
        targets = [monitor_suite.MonitorTarget("a", "/a/CIMonitor", "unused")]
        server = _FakeServer({"/a/CIMonitor": "queued"})

        # Deadline already in the past -> single poll round, then give up.
        states = monitor_suite.poll_targets(
            server, targets, poll_interval_seconds=0, deadline=0
        )

        assert states == {"a": None}

    def test_unknown_node_is_treated_as_pending(self):
        targets = [monitor_suite.MonitorTarget("a", "/a/CIMonitor", "unused")]
        server = _FakeServer({})  # get_node_state() returns None: node not found (yet)

        states = monitor_suite.poll_targets(
            server, targets, poll_interval_seconds=0, deadline=0
        )

        assert states == {"a": None}


class TestReadResults:
    """Unit tests for `read_results()`."""

    def test_pass(self, tmp_path, capsys):
        status_file = tmp_path / "ci_status.txt"
        status_file.write_text("Case: a\nRESULT=PASS\n", encoding="utf-8")
        target = monitor_suite.MonitorTarget("a", "/a/CIMonitor", str(status_file))

        results = monitor_suite.read_results([target], {"a": "complete"})

        assert results["a"]["status"] == "PASS"
        assert "RESULT=PASS" in capsys.readouterr().out

    def test_fail(self, tmp_path):
        status_file = tmp_path / "ci_status.txt"
        status_file.write_text("Case: a\nRESULT=FAIL\n", encoding="utf-8")
        target = monitor_suite.MonitorTarget("a", "/a/CIMonitor", str(status_file))

        results = monitor_suite.read_results([target], {"a": "aborted"})

        assert results["a"]["status"] == "FAIL"

    def test_timeout(self):
        target = monitor_suite.MonitorTarget(
            "a", "/a/CIMonitor", "/nonexistent/ci_status.txt"
        )

        results = monitor_suite.read_results([target], {"a": None})

        assert results["a"]["status"] == "TIMEOUT"

    def test_missing_status_file_after_terminal_state_is_fail(self, tmp_path):
        target = monitor_suite.MonitorTarget(
            "a", "/a/CIMonitor", str(tmp_path / "does_not_exist.txt")
        )

        results = monitor_suite.read_results([target], {"a": "complete"})

        assert results["a"]["status"] == "FAIL"


class TestWriteStepSummary:
    """Unit tests for `write_step_summary()`."""

    def test_writes_to_github_step_summary_when_set(self, tmp_path, monkeypatch):
        summary_file = tmp_path / "summary.md"
        monkeypatch.setenv("GITHUB_STEP_SUMMARY", str(summary_file))

        monitor_suite.write_step_summary({
            "a": {"status": "PASS", "detail": "..."},
            "b": {"status": "FAIL", "detail": "..."},
        })

        content = summary_file.read_text(encoding="utf-8")
        assert "| a | PASS |" in content
        assert "| b | FAIL |" in content

    def test_prints_when_github_step_summary_is_unset(self, monkeypatch, capsys):
        monkeypatch.delenv("GITHUB_STEP_SUMMARY", raising=False)

        monitor_suite.write_step_summary({"a": {"status": "PASS", "detail": "..."}})

        assert "| a | PASS |" in capsys.readouterr().out


class TestSuspendTargets:
    """Unit tests for `_suspend_targets()`/`_suspend_one()`."""

    def test_suspends_every_target(self):
        targets = [
            monitor_suite.MonitorTarget("a", "/a/CIMonitor", "x"),
            monitor_suite.MonitorTarget("b", "/b/CIMonitor", "y"),
        ]
        server = _FakeServer({})

        monitor_suite._suspend_targets(server, targets)

        assert server.suspended == ["/a/CIMonitor", "/b/CIMonitor"]

    def test_suspend_failure_on_one_target_does_not_stop_the_others(self):
        targets = [
            monitor_suite.MonitorTarget("a", "/a/CIMonitor", "x"),
            monitor_suite.MonitorTarget("b", "/b/CIMonitor", "y"),
        ]

        class _FlakyServer(_FakeServer):
            def suspend(self, path):
                if path == "/a/CIMonitor":
                    raise RuntimeError("ecflow unreachable")
                super().suspend(path)

        server = _FlakyServer({})

        # `/a/CIMonitor` failing to suspend must not stop `/b/CIMonitor` from
        # being attempted too.
        monitor_suite._suspend_targets(server, targets)

        assert server.suspended == ["/b/CIMonitor"]
