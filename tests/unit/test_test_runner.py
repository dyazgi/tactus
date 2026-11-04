#!/usr/bin/env python3
"""Unit tests for `tactus.test_runner`: summary gathering and pass/fail propagation."""

import json
from pathlib import Path

import pytest
import tomlkit

from tactus.test_runner import TestCases, run_test


def _make_test_cases(tmp_path: Path, test_dir_name: str = "test_dir") -> TestCases:
    """Build a bare `TestCases` instance for exercising summary-gathering logic.

    Bypasses `TestCases.__init__` (which needs a full CLI `args` namespace and a
    real config file on disk) since only `test_dir`/`config_name` are needed by
    `_gather_summaries`/`collect_summaries`/`overall_success`.
    """
    test_cases = TestCases.__new__(TestCases)
    test_cases.test_dir = str(tmp_path / test_dir_name)
    test_cases.config_name = "myconfig"
    test_cases.verbose = False
    test_cases.ial = {}
    Path(test_cases.test_dir).mkdir(parents=True, exist_ok=True)
    return test_cases


def _write_config_names(test_cases: TestCases, case_names) -> None:
    """Write the `<config_name>_config_names.toml` case-mapping file used by tests."""
    mapping = {"config_names": {case: case for case in case_names}}
    with open(
        f"{test_cases.test_dir}/{test_cases.config_name}_config_names.toml",
        "w",
        encoding="utf-8",
    ) as f:
        f.write(tomlkit.dumps(mapping))


def _write_summary(test_cases: TestCases, case_name: str, success: bool) -> Path:
    """Write a minimal `summary.json` with a completed analysis for `case_name`."""
    summary_file = Path(test_cases.test_dir) / f"{case_name}_summary.json"
    summary = {
        "analysis": {
            "success": success,
            "error_count": 0 if success else 1,
            "missing_count": 0,
            "result": "SUCCESS - 0 error(s)" if success else "FAILURE - 1 error(s)",
        },
        "tasks": {},
    }
    summary_file.write_text(json.dumps(summary), encoding="utf-8")
    return summary_file


@pytest.fixture
def patch_case_information(monkeypatch, tmp_path):
    """Patch `TestCases.get_case_information` to serve canned per-case metadata."""

    def _apply(cases: dict):
        """cases: dict of case_name -> (check: bool, json_file: str or None)."""

        def fake_get_case_information(config_file):
            case_name = Path(config_file).stem
            check, json_file = cases[case_name]
            return check, case_name, json_file, str(tmp_path / "references")

        monkeypatch.setattr(
            TestCases, "get_case_information", staticmethod(fake_get_case_information)
        )

    return _apply


class TestOverallSuccess:
    """Unit tests for `TestCases.overall_success()`."""

    def test_all_success(self, tmp_path, patch_case_information):
        test_cases = _make_test_cases(tmp_path)
        summary_a = _write_summary(test_cases, "case_a", success=True)
        summary_b = _write_summary(test_cases, "case_b", success=True)
        patch_case_information({
            "case_a": (True, str(summary_a)),
            "case_b": (True, str(summary_b)),
        })
        _write_config_names(test_cases, ["case_a", "case_b"])

        assert test_cases.overall_success() is True

    def test_one_failure_fails_all(self, tmp_path, patch_case_information):
        test_cases = _make_test_cases(tmp_path)
        summary_a = _write_summary(test_cases, "case_a", success=True)
        summary_b = _write_summary(test_cases, "case_b", success=False)
        patch_case_information({
            "case_a": (True, str(summary_a)),
            "case_b": (True, str(summary_b)),
        })
        _write_config_names(test_cases, ["case_a", "case_b"])

        assert test_cases.overall_success() is False

    def test_missing_summary_fails(self, tmp_path, patch_case_information):
        test_cases = _make_test_cases(tmp_path)
        missing_json = str(tmp_path / "does_not_exist_summary.json")
        patch_case_information({"case_a": (True, missing_json)})
        _write_config_names(test_cases, ["case_a"])

        assert test_cases.overall_success() is False

    def test_running_case_fails(self, tmp_path, patch_case_information):
        test_cases = _make_test_cases(tmp_path)
        summary_file = Path(test_cases.test_dir) / "case_a_summary.json"
        summary_file.write_text(json.dumps({"tasks": {}}), encoding="utf-8")
        patch_case_information({"case_a": (True, str(summary_file))})
        _write_config_names(test_cases, ["case_a"])

        assert test_cases.overall_success() is False

    def test_skipped_case_is_ignored(self, tmp_path, patch_case_information):
        test_cases = _make_test_cases(tmp_path)
        patch_case_information({"case_a": (False, None)})
        _write_config_names(test_cases, ["case_a"])

        assert test_cases.overall_success() is True

    def test_no_case_mapping_raises(self, tmp_path):
        test_cases = _make_test_cases(tmp_path)
        with pytest.raises(FileNotFoundError):
            test_cases.overall_success()


class _FakeArgs:
    """Minimal stand-in for the `tactus test` argparse namespace."""

    def __init__(self, **kwargs):
        defaults = {
            "config_file": None,
            "prepare_binaries": False,
            "list": False,
            "configure": False,
            "run": False,
            "verbose": False,
            "dry": False,
            "generate_refs": False,
        }
        defaults.update(kwargs)
        for key, value in defaults.items():
            setattr(self, key, value)


class TestRunTestReturnValue:
    """Unit tests for `run_test()`'s pass/fail return value."""

    def test_nothing_to_do_returns_false(self, monkeypatch):
        monkeypatch.setattr(TestCases, "__init__", lambda _self, **_kwargs: None)
        assert run_test(_FakeArgs()) is False

    def test_summary_branch_returns_overall_success(self, monkeypatch):
        monkeypatch.setattr(TestCases, "__init__", lambda _self, **_kwargs: None)
        monkeypatch.setattr(TestCases, "collect_summaries", lambda _self: None)
        args = _FakeArgs(config_file="dummy.toml")

        monkeypatch.setattr(TestCases, "overall_success", lambda _self: False)
        assert run_test(args) is False

        monkeypatch.setattr(TestCases, "overall_success", lambda _self: True)
        assert run_test(args) is True

    def test_configure_or_run_branch_returns_true_regardless_of_success(
        self, monkeypatch
    ):
        monkeypatch.setattr(TestCases, "__init__", lambda _self, **_kwargs: None)
        monkeypatch.setattr(TestCases, "execute", lambda _self, _args: None)
        args = _FakeArgs(config_file="dummy.toml", configure=True)
        assert run_test(args) is True

    def test_list_branch_returns_true(self, monkeypatch):
        monkeypatch.setattr(TestCases, "__init__", lambda _self, **_kwargs: None)
        monkeypatch.setattr(TestCases, "list", lambda _self: None)
        assert run_test(_FakeArgs(list=True)) is True

    def test_prepare_binaries_branch_returns_true(self, monkeypatch):
        monkeypatch.setattr(TestCases, "__init__", lambda _self, **_kwargs: None)
        monkeypatch.setattr(TestCases, "get_binaries", lambda _self: None)
        assert run_test(_FakeArgs(prepare_binaries=True)) is True
