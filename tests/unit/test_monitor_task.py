#!/usr/bin/env python3
"""Unit tests for the `CIMonitor` task (`tactus.tasks.monitor`)."""

import json
from pathlib import Path

import pytest

from tactus.config_parser import ParsedConfig
from tactus.derived_variables import set_times
from tactus.tasks.monitor import CIMonitor


@pytest.fixture(scope="module")
def basic_config(tmp_directory: str, default_config: ParsedConfig):
    """A resolvable config with a distinct scratch/casedir per test module."""
    config = default_config.copy(update=set_times(default_config))
    return config.copy(
        update={
            "platform": {
                "scratch": tmp_directory,
                "unix_group": "",
            },
        }
    )


def _config_for_case(basic_config: ParsedConfig, case_name: str, context: str, **args):
    """Build a config for `case_name` with `task.args.context` (and extras) set."""
    all_args = {"context": context, **args}
    args_string = ";".join(f"{key}={value}" for key, value in all_args.items())
    return basic_config.copy(
        update={
            "general": {"case": case_name},
            "task": {"args": all_args},
        }
    ), args_string


class TestCIMonitorTestbedContext:
    """Unit tests for the `testbed` context (default): reuses the summary.json."""

    def test_writes_pass_when_summary_analysis_succeeded(self, basic_config, tmp_path):
        config, _ = _config_for_case(basic_config, "case_pass", context="testbed")
        summary_file = tmp_path / "case_pass_summary.json"
        summary_file.write_text(
            json.dumps({"analysis": {"success": True, "result": "SUCCESS - 0 error(s)"}}),
            encoding="utf-8",
        )
        config = config.copy(
            update={
                "reference_checker": {"summary": {"json": {"file": str(summary_file)}}}
            }
        )

        task = CIMonitor(config)
        task.execute()

        status_file = Path(task.status_file)
        assert status_file.name == "ci_status.txt"
        content = status_file.read_text(encoding="utf-8")
        assert content.strip().splitlines()[-1] == "RESULT=PASS"

    def test_writes_fail_when_summary_analysis_failed(self, basic_config, tmp_path):
        config, _ = _config_for_case(basic_config, "case_fail", context="testbed")
        summary_file = tmp_path / "case_fail_summary.json"
        summary_file.write_text(
            json.dumps({
                "analysis": {"success": False, "result": "FAILURE - 1 error(s)"}
            }),
            encoding="utf-8",
        )
        config = config.copy(
            update={
                "reference_checker": {"summary": {"json": {"file": str(summary_file)}}}
            }
        )

        task = CIMonitor(config)
        task.execute()

        content = Path(task.status_file).read_text(encoding="utf-8")
        assert content.strip().splitlines()[-1] == "RESULT=FAIL"

    def test_writes_fail_when_summary_is_missing(self, basic_config, tmp_path):
        config, _ = _config_for_case(basic_config, "case_missing", context="testbed")
        missing_summary_file = tmp_path / "does_not_exist_summary.json"
        config = config.copy(
            update={
                "reference_checker": {
                    "summary": {"json": {"file": str(missing_summary_file)}}
                }
            }
        )

        task = CIMonitor(config)
        task.execute()

        content = Path(task.status_file).read_text(encoding="utf-8")
        assert content.strip().splitlines()[-1] == "RESULT=FAIL"

    def test_writes_fail_when_summary_has_no_analysis_yet(self, basic_config, tmp_path):
        config, _ = _config_for_case(basic_config, "case_running", context="testbed")
        summary_file = tmp_path / "case_running_summary.json"
        summary_file.write_text(json.dumps({"tasks": {}}), encoding="utf-8")
        config = config.copy(
            update={
                "reference_checker": {"summary": {"json": {"file": str(summary_file)}}}
            }
        )

        task = CIMonitor(config)
        task.execute()

        content = Path(task.status_file).read_text(encoding="utf-8")
        assert content.strip().splitlines()[-1] == "RESULT=FAIL"


class TestCIMonitorCompileContext:
    """Unit tests for the `compile` context: reuses ecFlow's own task state."""

    def test_writes_pass_when_build_task_completed(self, basic_config, monkeypatch):
        config, _ = _config_for_case(
            basic_config,
            "IAL_test_compile",
            context="compile",
            build_path="/IAL_test_compile/Compilation/TactusBundleBuild/R64/TactusBundleBuild",
        )

        class _FakeServer:
            def __init__(self, _config):
                pass

            def get_node_state(self, _path):
                return "complete"

        monkeypatch.setattr("tactus.tasks.monitor.EcflowServer", _FakeServer)

        task = CIMonitor(config)
        task.execute()

        content = Path(task.status_file).read_text(encoding="utf-8")
        assert content.strip().splitlines()[-1] == "RESULT=PASS"

    def test_writes_fail_when_build_task_aborted(self, basic_config, monkeypatch):
        config, _ = _config_for_case(
            basic_config,
            "IAL_test_compile_aborted",
            context="compile",
            build_path="/IAL_test_compile_aborted/Compilation/TactusBundleBuild/R64/"
            "TactusBundleBuild",
        )

        class _FakeServer:
            def __init__(self, _config):
                pass

            def get_node_state(self, _path):
                return "aborted"

        monkeypatch.setattr("tactus.tasks.monitor.EcflowServer", _FakeServer)

        task = CIMonitor(config)
        task.execute()

        content = Path(task.status_file).read_text(encoding="utf-8")
        assert content.strip().splitlines()[-1] == "RESULT=FAIL"

    def test_writes_fail_when_build_path_is_missing(self, basic_config):
        config, _ = _config_for_case(
            basic_config, "IAL_test_compile_no_path", context="compile"
        )

        task = CIMonitor(config)
        task.execute()

        content = Path(task.status_file).read_text(encoding="utf-8")
        assert content.strip().splitlines()[-1] == "RESULT=FAIL"
