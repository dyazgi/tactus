#!/usr/bin/env python3
"""Follow a `tactus`-submitted suite's `CIMonitor` checkpoint(s) for CI.

Run after `tactus compile`/`tactus test -c <config> -m -r` has submitted a suite to
ecFlow. Polls the suite's `CIMonitor` task(s) (see `tactus.tasks.monitor.CIMonitor`)
via `tactus.scheduler.EcflowServer` until each reaches a terminal ecFlow state (or
the configured timeout elapses), then reads each `ci_status.txt` marker file,
echoes it to stdout (so it shows up in the GitHub Actions log) and writes a
markdown PASS/FAIL summary to `$GITHUB_STEP_SUMMARY` (or stdout, if that
environment variable is unset -- e.g. when run locally outside CI).

Exit code is 0 iff every monitored target reported `RESULT=PASS`, 1 otherwise
(including on timeout -- printed/summarized as `TIMEOUT`, distinct from `FAIL`,
for diagnosability). On timeout, or on receiving `SIGTERM` (GitHub Actions
cancellation), any target still awaited is suspended via
`EcflowServer.suspend()` before exiting, so orphaned SLURM jobs stop running
unattended -- both paths funnel through the same `_suspend_targets` call.

Usage:
    poetry run python scripts/ci/monitor_suite.py --mode compile
        --config <compile_config.toml>
    poetry run python scripts/ci/monitor_suite.py --mode testbed
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

#: ecFlow states considered terminal (i.e. the `CIMonitor` checkpoint task ran).
TERMINAL_STATES = {"complete", "aborted"}


@dataclass
class MonitorTarget:
    """One `CIMonitor` task to poll, and where to read its result once it is done."""

    #: Human-readable label (case name, or "compile:<precision>").
    name: str
    #: Absolute ecFlow path of the `CIMonitor` task itself.
    ecf_path: str
    #: Resolved path of the `ci_status.txt` file it writes.
    status_file: str

    @property
    def casedir(self) -> str:
        """The resolved `@CASEDIR@` this target's `ci_status.txt` lives under.

        Also where the reference checker's own `checks/` subtree (`summary.json`,
        `summary.txt`, per-task `.diff` files - see
        `tactus/data/config_files/include/reference_checker.toml`) lives for a
        testbed case, so this is the right root for a CI artifact upload: the
        runner's local checkout only ever holds generated `*.toml` configs, never
        these result files.

        """
        return os.path.dirname(self.status_file)


def parse_args(argv: Optional[list] = None) -> argparse.Namespace:
    """Parse command line arguments.

    Args:
        argv: Command line arguments. Defaults to `sys.argv[1:]`.

    Returns:
        argparse.Namespace: Parsed arguments.

    """
    parser = argparse.ArgumentParser(
        description=(
            "Follow a tactus suite's CIMonitor checkpoint(s) and summarize "
            "PASS/FAIL for GitHub Actions."
        )
    )
    parser.add_argument(
        "--mode",
        choices=["compile", "testbed"],
        required=True,
        help="Whether to follow a compile suite (one CIMonitor per precision leg) "
        "or a testbed run (one CIMonitor per selected case).",
    )
    parser.add_argument(
        "--config",
        required=True,
        help="For --mode compile: the resolved compile-suite config file (as "
        "produced by `tactus compile`). For --mode testbed: the same test-runner "
        "config file passed to `tactus test -c` (after `-m -r` have been run).",
    )
    parser.add_argument(
        "--poll-interval-seconds",
        type=int,
        default=60,
        help="Seconds to wait between two polls of the CIMonitor task(s).",
    )
    parser.add_argument(
        "--timeout-minutes",
        type=int,
        default=240,
        help="Hard deadline: give up (and suspend anything still pending) after "
        "this many minutes.",
    )
    parser.add_argument(
        "--artifact-manifest",
        default=None,
        help="If given, write each target's resolved @CASEDIR@ (one per line, "
        "deduplicated) to this file. @CASEDIR@ is where ci_status.txt, the "
        "reference checker's summary.json/.txt and its per-task .diff files all "
        "actually live (on HPC scratch) - NOT under the runner's local checkout, "
        "so a caller that wants to upload these as a CI artifact should glob "
        "against the paths in this manifest, not guess a path.",
    )
    return parser.parse_args(argv)


def _resolve_casedir(config: ParsedConfig) -> str:
    """Resolve `@CASEDIR@` (`system.casedir`) for one already-loaded case config.

    Args:
        config (ParsedConfig): A case's own configuration (as saved to disk by
            `tactus case`/`tactus test -m`), already carrying its own
            `general.case`.

    Returns:
        str: The resolved `system.casedir` value.

    """
    return Platform(config).get_system_value("casedir")


def discover_compile_targets(config_file: str) -> list[MonitorTarget]:
    """Discover the `CIMonitor` task(s) for a compile suite.

    One target per precision leg (`R64`, and `R32` if
    `submission.precision == "R32"`), mirroring
    `tactus.suites.tactus_suite_components.CompilationFamily`.

    Args:
        config_file (str): The resolved compile-suite config file.

    Returns:
        list[MonitorTarget]: One target per precision leg.

    """
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
        )
        for precision in precisions
    ]


def discover_testbed_targets(config_file: str) -> list[MonitorTarget]:
    """Discover the `CIMonitor` task(s) for a testbed run: one per selected case.

    Reuses `tactus.test_runner.TestCases` (the same class backing `tactus test`)
    to enumerate the cases and their generated per-case config files, rather than
    re-implementing that lookup.

    Args:
        config_file (str): The test-runner config file, as passed to
            `tactus test -c`.

    Returns:
        list[MonitorTarget]: One target per case selected by that config file.

    """
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
            )
        )
    return targets


def poll_targets(
    server: EcflowServer,
    targets: list[MonitorTarget],
    poll_interval_seconds: int,
    deadline: float,
) -> dict[str, Optional[str]]:
    """Poll each target's `CIMonitor` ecFlow state until terminal or `deadline`.

    Args:
        server (EcflowServer): Connection to the ecFlow server the suite(s) run on.
        targets (list[MonitorTarget]): Targets to poll.
        poll_interval_seconds (int): Seconds to sleep between polling rounds.
        deadline (float): `time.time()`-comparable deadline; polling stops once
            reached, regardless of whether all targets are done.

    Returns:
        dict[str, str or None]: target.name -> final ecFlow state (as `str`), or
            `None` if the deadline was reached first (timeout).

    """
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
    """Read and echo each target's `ci_status.txt`, classifying PASS/FAIL/TIMEOUT.

    Args:
        targets (list[MonitorTarget]): Targets that were polled.
        states (dict[str, str or None]): Result of `poll_targets`.

    Returns:
        dict[str, dict]: target.name -> {"status": "PASS"|"FAIL"|"TIMEOUT",
            "detail": str}.

    """
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
    """Write a markdown PASS/FAIL summary to `$GITHUB_STEP_SUMMARY`, or stdout.

    Args:
        results (dict[str, dict]): Result of `read_results`.

    """
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
    """Write each target's `casedir` (deduplicated, order-preserving) to a file.

    Args:
        targets (list[MonitorTarget]): Targets whose `casedir` to record.
        manifest_path (str): File to write, one `casedir` per line.

    """
    casedirs = list(dict.fromkeys(target.casedir for target in targets))
    with open(manifest_path, "w", encoding="utf-8") as f:
        f.write("\n".join(casedirs) + ("\n" if casedirs else ""))
    logger.info(
        "Wrote {} casedir(s) to artifact manifest {}", len(casedirs), manifest_path
    )


def _suspend_one(server: EcflowServer, target: MonitorTarget) -> None:
    """Suspend a single target's ecFlow node (best-effort, logs but never raises).

    Args:
        server (EcflowServer): Connection to the ecFlow server.
        target (MonitorTarget): Target to suspend.

    """
    try:
        server.suspend(target.ecf_path)
    except RuntimeError:
        logger.exception("Failed to suspend {}", target.ecf_path)


def _suspend_targets(server: EcflowServer, targets: list[MonitorTarget]) -> None:
    """Suspend every target's ecFlow node.

    Shared by the timeout path and the `SIGTERM` handler, so orphaned SLURM jobs
    stop rather than run unattended, regardless of *why* we stopped waiting.

    Args:
        server (EcflowServer): Connection to the ecFlow server.
        targets (list[MonitorTarget]): Targets to suspend.

    """
    for target in targets:
        _suspend_one(server, target)


def main(argv: Optional[list] = None) -> int:
    """Program's main routine.

    Args:
        argv: Command line arguments. Defaults to `sys.argv[1:]`.

    Returns:
        int: Process exit code (0 iff every target reported `RESULT=PASS`).

    """
    args = parse_args(argv)

    # Only used below to construct EcflowServer(config), which just needs
    # scheduler.ecfvars.* (host/port) - no date-derived macros. Don't call
    # set_times() on it: for --mode testbed, args.config is the top-level
    # test-runner config (e.g. atos_bologna_CY50t2.toml), which has no
    # [general.times] section at all (each case's own generated config
    # carries its own, resolved separately in discover_testbed_targets
    # below) - set_times() would crash on that missing section.
    config = ParsedConfig.from_file(args.config, json_schema={})

    if args.mode == "compile":
        targets = discover_compile_targets(args.config)
    else:
        targets = discover_testbed_targets(args.config)

    server = EcflowServer(config)
    pending_targets = list(targets)

    def _handle_sigterm(signum, _frame):
        """Suspend anything still pending, then exit non-zero (GHA cancellation)."""
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
