#!/usr/bin/env python3
"""Poll a tactus-submitted suite's CIMonitor task(s) and summarize PASS/FAIL for CI.

Run after `tactus compile`/`tactus test -c <config> -m -r`. Exit code is 0 iff
every target reported RESULT=PASS. On timeout or SIGTERM, any pending target
is suspended so orphaned SLURM jobs don't run unattended.

Usage:
    poetry run python scripts/ci/monitor_suite.py --mode compile \
        --config <compile_config.toml>
    poetry run python scripts/ci/monitor_suite.py --mode testbed \
        --config <test_runner_config.toml>
"""

from __future__ import annotations

import argparse
import os
import signal
import sys
import time
from dataclasses import dataclass
from typing import Optional

import tomli

from tactus import GeneralConstants
from tactus.config_parser import ParsedConfig
from tactus.logs import logger
from tactus.scheduler import EcflowServer
from tactus.tasks.monitor import STATUS_FILENAME
from tactus.test_runner import TestCases
from tactus.toolbox import Platform

logger.enable(GeneralConstants.PACKAGE_NAME)

TERMINAL_STATES = {"complete", "aborted"}


@dataclass
class MonitorTarget:
    """One CIMonitor task to poll, and where to read its result."""

    name: str
    ecf_path: str
    status_file: str
    # Fully-resolved config this target came from - unlike the top-level
    # --config (for --mode testbed), this one carries [platform]/[system],
    # so it's safe to build an EcflowServer from it.
    config_file: str = ""

    @property
    def casedir(self) -> str:
        """Resolved @CASEDIR@ this target's result files live under."""
        return os.path.dirname(self.status_file)


def parse_args(argv: Optional[list] = None) -> argparse.Namespace:
    """Parse command-line arguments."""
    parser = argparse.ArgumentParser(
        description="Follow a tactus suite's CIMonitor checkpoint(s) and summarize."
    )
    parser.add_argument("--mode", choices=["compile", "testbed"], required=True)
    parser.add_argument("--config", required=True)
    parser.add_argument("--poll-interval-seconds", type=int, default=60)
    parser.add_argument("--timeout-minutes", type=int, default=240)
    parser.add_argument(
        "--artifact-manifest",
        default=None,
        help="If given, write each target's @CASEDIR@ (deduplicated) to this file.",
    )
    return parser.parse_args(argv)


def _resolve_casedir(config: ParsedConfig) -> str:
    return Platform(config).get_system_value("casedir")


def discover_compile_targets(config_file: str) -> list[MonitorTarget]:
    """One target per precision leg (R64, and R32 if submission.precision == R32)."""
    config = ParsedConfig.from_file(config_file, json_schema={})
    platform = Platform(config)
    case_name = platform.substitute(config.get("general.case"))
    config = config.copy(update={"general": {"case": case_name}})
    casedir = _resolve_casedir(config)

    precisions = ["R64"]
    if config.get("submission.precision") == "R32":
        precisions.append("R32")

    return [
        MonitorTarget(
            name=f"compile:{precision}",
            ecf_path=(
                f"/{case_name}/Compilation/TactusBundleBuild/{precision}/CIMonitor"
            ),
            status_file=os.path.join(casedir, STATUS_FILENAME),
            config_file=config_file,
        )
        for precision in precisions
    ]


def discover_testbed_targets(config_file: str) -> list[MonitorTarget]:
    """One target per case selected by the test-runner config."""
    args = argparse.Namespace(
        config_file=config_file, verbose=False, dry=False, generate_refs=False
    )
    test_cases = TestCases(args=args)

    case_names_file = f"{test_cases.test_dir}/{test_cases.config_name}_config_names.toml"
    with open(case_names_file, "rb") as f:
        config_names = tomli.load(f)["config_names"]

    targets = []
    for case, config_name in config_names.items():
        case_config_file = f"{test_cases.test_dir}/{config_name}.toml"
        _check, ecf_case_name, _json_file, _refs_dir = TestCases.get_case_information(
            case_config_file
        )
        case_config = ParsedConfig.from_file(case_config_file, json_schema={})
        casedir = _resolve_casedir(case_config)
        targets.append(
            MonitorTarget(
                name=case,
                ecf_path=f"/{ecf_case_name}/CIMonitor",
                status_file=os.path.join(casedir, STATUS_FILENAME),
                config_file=case_config_file,
            )
        )
    return targets


def poll_targets(
    server: EcflowServer,
    targets: list[MonitorTarget],
    poll_interval_seconds: int,
    deadline: float,
) -> dict[str, Optional[str]]:
    """Poll until every target is terminal or the deadline passes."""
    states: dict[str, Optional[str]] = dict.fromkeys(target.name for target in targets)
    pending = list(targets)

    while pending:
        still_pending = []
        for target in pending:
            state = server.get_node_state(target.ecf_path)
            state_str = str(state) if state is not None else None
            if state_str in TERMINAL_STATES:
                states[target.name] = state_str
                logger.info(
                    "{}: CIMonitor ({}) reached terminal state '{}'",
                    target.name,
                    target.ecf_path,
                    state_str,
                )
            else:
                still_pending.append(target)
        pending = still_pending

        if not pending:
            break
        if time.time() >= deadline:
            logger.warning(
                "Timed out waiting for: {}", [target.name for target in pending]
            )
            break
        time.sleep(poll_interval_seconds)

    return states


def read_results(
    targets: list[MonitorTarget], states: dict[str, Optional[str]]
) -> dict[str, dict]:
    """Read and echo each target's ci_status.txt, classifying PASS/FAIL/TIMEOUT."""
    results = {}
    for target in targets:
        state = states.get(target.name)
        if state is None:
            detail = f"TIMEOUT waiting for {target.ecf_path} to reach a terminal state"
            print(f"----- {target.name}: {detail} -----")
            results[target.name] = {"status": "TIMEOUT", "detail": detail}
            continue

        try:
            with open(target.status_file, "r", encoding="utf-8") as f:
                content = f.read()
        except FileNotFoundError:
            detail = f"CIMonitor reached '{state}' but {target.status_file} is missing"
            print(f"----- {target.name}: {detail} -----")
            results[target.name] = {"status": "FAIL", "detail": detail}
            continue

        print(f"----- {target.name}: {target.status_file} -----")
        print(content)

        last_line = content.strip().splitlines()[-1] if content.strip() else ""
        status = "PASS" if last_line == "RESULT=PASS" else "FAIL"
        results[target.name] = {"status": status, "detail": content}

    return results


def write_step_summary(results: dict[str, dict]) -> None:
    """Write a markdown PASS/FAIL table to $GITHUB_STEP_SUMMARY, or stdout."""
    lines = ["## tactus CI monitor summary", "", "| Target | Result |", "| --- | --- |"]
    for name, result in results.items():
        lines.append(f"| {name} | {result['status']} |")
    markdown = "\n".join(lines) + "\n"

    summary_path = os.environ.get("GITHUB_STEP_SUMMARY")
    if summary_path:
        with open(summary_path, "a", encoding="utf-8") as f:
            f.write(markdown)
        logger.info("Wrote summary to {}", summary_path)
    else:
        logger.info("GITHUB_STEP_SUMMARY not set; printing summary instead")
        print(markdown)


def write_artifact_manifest(targets: list[MonitorTarget], manifest_path: str) -> None:
    """Write each target's casedir (deduplicated) to a file, one per line."""
    casedirs = list(dict.fromkeys(target.casedir for target in targets))
    with open(manifest_path, "w", encoding="utf-8") as f:
        f.write("\n".join(casedirs) + ("\n" if casedirs else ""))
    logger.info(
        "Wrote {} casedir(s) to artifact manifest {}", len(casedirs), manifest_path
    )


def _suspend_one(server: EcflowServer, target: MonitorTarget) -> None:
    try:
        server.suspend(target.ecf_path)
    except RuntimeError:
        logger.exception("Failed to suspend {}", target.ecf_path)


def _suspend_targets(server: EcflowServer, targets: list[MonitorTarget]) -> None:
    for target in targets:
        _suspend_one(server, target)


def main(argv: Optional[list] = None) -> int:
    """Entry point."""
    args = parse_args(argv)

    if args.mode == "compile":
        targets = discover_compile_targets(args.config)
    else:
        targets = discover_testbed_targets(args.config)

    if not targets:
        logger.warning("No targets to monitor for {}", args.config)
        return 0

    # EcflowServer needs [platform]/[system] - args.config (for --mode
    # testbed) doesn't have those, so reuse a target's own resolved config.
    server_config = ParsedConfig.from_file(targets[0].config_file, json_schema={})
    server = EcflowServer(server_config)
    pending_targets = list(targets)

    def _handle_sigterm(signum, _frame):
        logger.warning(
            "Received signal {}; suspending in-flight targets and exiting", signum
        )
        _suspend_targets(server, pending_targets)
        sys.exit(1)

    signal.signal(signal.SIGTERM, _handle_sigterm)

    deadline = time.time() + args.timeout_minutes * 60
    states = poll_targets(server, targets, args.poll_interval_seconds, deadline)
    pending_targets[:] = [target for target in targets if states.get(target.name) is None]
    if pending_targets:
        _suspend_targets(server, pending_targets)

    results = read_results(targets, states)
    write_step_summary(results)

    if args.artifact_manifest:
        write_artifact_manifest(targets, args.artifact_manifest)

    return 0 if all(result["status"] == "PASS" for result in results.values()) else 1


if __name__ == "__main__":
    sys.exit(main())
