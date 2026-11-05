#!/usr/bin/env python3
"""Poll a tactus-submitted suite's CIMonitor task(s) and summarize PASS/FAIL for CI.

Run after `tactus compile`/`tactus test -c <config> -m -r`. Exit code is 0 iff
every target reported RESULT=PASS. Never waits forever on a dead target:
 - a suite with an aborted task (CIMonitor still queued after ABORT_POLLS polls)
   is suspended, its running jobs are killed and it is reported FAIL;
 - a target whose host suite failed (its Mirror node can never complete) is
   reported BLOCKED;
 - on timeout or SIGTERM, pending suites are suspended and killed.

Usage (on the HPC, in the project venv):
    python3 scripts/ci/monitor_suite.py --mode compile --config <compile_config.toml>
    python3 scripts/ci/monitor_suite.py --mode testbed --config <test_runner_config.toml>
    python3 scripts/ci/monitor_suite.py --mode ... --config ... --print-suites
"""

from __future__ import annotations

import argparse
import os
import signal
import sys
import time
from dataclasses import dataclass
from typing import Optional

import ecflow
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
# Polls an aborted task must persist with CIMonitor still queued: lets CIMonitor
# report a ReferenceCheck abort itself and ignores ecFlow's automatic retries.
ABORT_POLLS = 2


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
    # Target whose suite this one's Mirror node waits on, and the node it mirrors.
    host: Optional[str] = None
    mirror_path: Optional[str] = None

    @property
    def casedir(self) -> str:
        """Resolved @CASEDIR@ this target's result files live under."""
        return os.path.dirname(self.status_file)

    @property
    def suite_path(self) -> str:
        """Absolute ecFlow path of the suite this target belongs to."""
        return "/" + self.ecf_path.split("/")[1]


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
    parser.add_argument(
        "--failed-outputs-manifest",
        default=None,
        help="If given, write '<output file>\\t<relative name>' of every aborted "
        "task's job output to this file.",
    )
    parser.add_argument(
        "--print-suites",
        action="store_true",
        help="Only print the ecFlow suite name of each target, then exit.",
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


def _mirror_of(
    test_cases: TestCases, case: str, case_config: ParsedConfig, config_names: dict
) -> tuple[Optional[str], Optional[str]]:
    """Return (host target name, mirrored node path) if this case waits on its host."""
    host = test_cases.cases.get(case, {}).get("host")
    if host not in config_names or not case_config.get(
        "suite_control.mirror_suite", False
    ):
        return None, None
    try:
        start = case_config["general.times.start"]
        path = Platform(case_config).substitute(
            case_config["scheduler.mirror_suite.remote_path"],
            basetime=start,
            validtime=start,
        )
    except Exception:  # noqa: BLE001 - best effort: fall back to host-only blocking
        logger.exception("Could not resolve the mirrored node of {}", case)
        path = None
    return host, path


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
        host, mirror_path = _mirror_of(test_cases, case, case_config, config_names)
        targets.append(
            MonitorTarget(
                name=case,
                ecf_path=f"/{ecf_case_name}/CIMonitor",
                status_file=os.path.join(casedir, STATUS_FILENAME),
                config_file=case_config_file,
                host=host,
                mirror_path=mirror_path,
            )
        )
    return targets


def _read_result(target: MonitorTarget, state: str) -> dict:
    """Read and echo a terminal target's ci_status.txt, classifying PASS/FAIL."""
    try:
        with open(target.status_file, "r", encoding="utf-8") as f:
            content = f.read()
    except FileNotFoundError:
        detail = f"CIMonitor reached '{state}' but {target.status_file} is missing"
        print(f"----- {target.name}: {detail} -----")
        return {"status": "FAIL", "detail": detail}

    print(f"----- {target.name}: {target.status_file} -----")
    print(content)

    last_line = content.strip().splitlines()[-1] if content.strip() else ""
    return {
        "status": "PASS" if last_line == "RESULT=PASS" else "FAIL",
        "detail": content,
    }


def _aborted_tasks(server: EcflowServer, suite) -> list:
    return [
        task
        for task in server.get_all_tasks(suite)
        if task.get_state() == ecflow.State.aborted
    ]


def _task_output(suite, task) -> Optional[tuple[str, str]]:
    """(job output file, relative name) of a task's latest try, if resolvable."""
    try:
        jobout = suite.find_variable("ECF_JOBOUT").value()
        tryno = task.find_gen_variable("ECF_TRYNO").value()
    except Exception:  # noqa: BLE001 - best effort
        logger.exception("Could not resolve job output of {}", task.get_abs_node_path())
        return None
    if not jobout:
        return None
    name = task.get_abs_node_path()
    path = jobout.replace("%ECF_NAME%", name).replace("%ECF_TRYNO%", tryno)
    return path, f"{name.lstrip('/')}.{tryno}"


def _collect_failed_outputs(
    server: EcflowServer, defs, target: MonitorTarget, failed_outputs: dict
) -> None:
    suite = defs.find_abs_node(target.suite_path)
    if suite is None:
        return
    for task in _aborted_tasks(server, suite):
        output = _task_output(suite, task)
        if output is not None:
            failed_outputs[output[0]] = output[1]


def _stop_suite(server: EcflowServer, suite_path: str, stopped: set) -> None:
    """Suspend a suite (no new submissions) and kill its running jobs, once."""
    if suite_path in stopped:
        return
    stopped.add(suite_path)
    logger.info("Suspending and killing {}", suite_path)
    for name in ("suspend", "kill"):
        try:
            getattr(server.ecf_client, name)(suite_path)
        except RuntimeError:
            logger.exception("{} failed for {}", name, suite_path)


def _blocked_reason(
    target: MonitorTarget, by_name: dict, results: dict, defs
) -> Optional[str]:
    """Reason this target can never start, because its host suite failed."""
    host = by_name.get(target.host) if target.host else None
    if host is None or results.get(host.name, {}).get("status", "PASS") == "PASS":
        return None
    if target.mirror_path:
        mirrored = defs.find_abs_node(target.mirror_path)
        if mirrored is not None and str(mirrored.get_state()) == "complete":
            return None
    return f"upstream {host.name} failed"


def watch_targets(
    server: EcflowServer,
    targets: list[MonitorTarget],
    poll_interval_seconds: int,
    deadline: float,
    pending: list[MonitorTarget],
    stopped: set,
    failed_outputs: dict,
) -> dict[str, dict]:
    """Poll until every target has a result: terminal, dead, blocked or timed out."""
    by_name = {target.name: target for target in targets}
    results: dict[str, dict] = {}
    abort_polls = dict.fromkeys(by_name, 0)

    while pending:
        server.ecf_client.sync_local()
        defs = server.ecf_client.get_defs()

        for target in list(pending):
            ci_node = defs.find_abs_node(target.ecf_path)
            ci_state = str(ci_node.get_state()) if ci_node is not None else None
            if ci_state in TERMINAL_STATES:
                logger.info("{}: CIMonitor reached '{}'", target.name, ci_state)
                results[target.name] = _read_result(target, ci_state)
                if results[target.name]["status"] != "PASS":
                    _collect_failed_outputs(server, defs, target, failed_outputs)
                pending.remove(target)
                continue

            suite = defs.find_abs_node(target.suite_path)
            blocked = _blocked_reason(target, by_name, results, defs)
            reason = None
            if suite is not None:
                aborted = _aborted_tasks(server, suite)
                if aborted and ci_state not in ("submitted", "active"):
                    abort_polls[target.name] += 1
                    if abort_polls[target.name] >= ABORT_POLLS:
                        reason = f"aborted task {aborted[0].get_abs_node_path()}"
                else:
                    abort_polls[target.name] = 0
                if ci_node is None and str(suite.get_state()) == "complete":
                    reason = "suite complete but has no CIMonitor node"

            if blocked is None and reason is None:
                continue

            status = "BLOCKED" if blocked is not None else "FAIL"
            note = blocked if blocked is not None else reason
            print(f"----- {target.name}: {status} - {note} -----")
            results[target.name] = {"status": status, "detail": note, "note": note}
            _collect_failed_outputs(server, defs, target, failed_outputs)
            _stop_suite(server, target.suite_path, stopped)
            pending.remove(target)

        if not pending:
            break
        if time.time() >= deadline:
            for target in pending:
                note = f"TIMEOUT waiting for {target.ecf_path} to reach a terminal state"
                print(f"----- {target.name}: {note} -----")
                results[target.name] = {"status": "TIMEOUT", "detail": note, "note": note}
                _collect_failed_outputs(server, defs, target, failed_outputs)
                _stop_suite(server, target.suite_path, stopped)
            pending.clear()
            break
        time.sleep(poll_interval_seconds)

    return results


def write_step_summary(results: dict[str, dict]) -> None:
    """Write a markdown PASS/FAIL table to $GITHUB_STEP_SUMMARY, or stdout."""
    lines = [
        "## tactus CI monitor summary",
        "",
        "| Target | Result | Note |",
        "| --- | --- | --- |",
    ]
    for name, result in results.items():
        lines.append(f"| {name} | {result['status']} | {result.get('note', '')} |")
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


def write_failed_outputs_manifest(failed_outputs: dict, manifest_path: str) -> None:
    """Write '<output file>\\t<relative name>' per aborted task, one per line."""
    with open(manifest_path, "w", encoding="utf-8") as f:
        for path, rel in failed_outputs.items():
            f.write(f"{path}\t{rel}\n")
    logger.info(
        "Wrote {} failed-task output(s) to {}", len(failed_outputs), manifest_path
    )


def main(argv: Optional[list] = None) -> int:
    """Entry point."""
    args = parse_args(argv)

    if args.mode == "compile":
        targets = discover_compile_targets(args.config)
    else:
        targets = discover_testbed_targets(args.config)

    if args.print_suites:
        for suite_path in dict.fromkeys(target.suite_path for target in targets):
            print(suite_path.lstrip("/"))
        return 0

    if not targets:
        logger.warning("No targets to monitor for {}", args.config)
        return 0

    # EcflowServer needs [platform]/[system] - args.config (for --mode
    # testbed) doesn't have those, so reuse a target's own resolved config.
    server_config = ParsedConfig.from_file(targets[0].config_file, json_schema={})
    server = EcflowServer(server_config)
    pending = list(targets)
    stopped: set = set()
    failed_outputs: dict = {}

    def _handle_sigterm(signum, _frame):
        logger.warning("Received signal {}; stopping in-flight suites and exiting", signum)
        for target in pending:
            _stop_suite(server, target.suite_path, stopped)
        sys.exit(1)

    signal.signal(signal.SIGTERM, _handle_sigterm)

    deadline = time.time() + args.timeout_minutes * 60
    results = watch_targets(
        server,
        targets,
        args.poll_interval_seconds,
        deadline,
        pending,
        stopped,
        failed_outputs,
    )
    write_step_summary(results)

    if args.artifact_manifest:
        write_artifact_manifest(targets, args.artifact_manifest)
    if args.failed_outputs_manifest:
        write_failed_outputs_manifest(failed_outputs, args.failed_outputs_manifest)

    return 0 if all(result["status"] == "PASS" for result in results.values()) else 1


if __name__ == "__main__":
    sys.exit(main())
