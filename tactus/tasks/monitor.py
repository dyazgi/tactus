"""CI checkpoint task: writes a canonical PASS/FAIL marker file for CI to follow."""

import json
import os
from datetime import datetime

from ..logs import logger
from ..os_utils import tactusmakedirs
from ..scheduler import EcflowServer
from .base import Task

#: Name of the marker file written by :class:`CIMonitor` (see class docstring).
STATUS_FILENAME = "ci_status.txt"


class CIMonitor(Task):
    """Checkpoint task writing a final ``ci_status.txt`` PASS/FAIL marker for CI.

    This is a "checkpoint" task, not a long-lived streamer: it is triggered once
    the task/family it is monitoring reaches a terminal ecFlow state (``complete``
    *or* ``aborted`` -- see how it is wired in with an explicit
    ``... == complete or ... == aborted`` trigger expression in
    ``tactus.suites.tactus_suite_components.CompilationFamily`` and
    ``tactus.suites.tactus.TactusSuiteDefinition``), evaluates the real pass/fail
    outcome for its context, and writes it to a small text file that
    ``scripts/ci/monitor_suite.py`` reads back to summarize the run for GitHub
    Actions. This task deliberately never raises: it always ends up ``complete``
    in ecFlow regardless of the PASS/FAIL outcome it reports, so that the
    follower script only ever has to poll for one terminal state (this task's
    own) instead of having to distinguish "the thing I'm watching aborted" from
    "the thing I'm watching failed its own check".

    Two contexts are supported, selected via the ``context`` ``ARGS`` key (i.e.
    ``task.args.context`` once parsed by the ecFlow job template -- the same
    ``ARGS``-as-``key=value;...`` convention already used e.g. by
    ``TactusBundleBuild``'s ``prec`` argument):

    - ``compile``: success is "did the preceding ``TactusBundleBuild`` task
      complete", read directly from ecFlow's own state for that task's node
      (``task.args.build_path``) via
      :meth:`tactus.scheduler.EcflowServer.get_node_state` -- not
      reimplemented, since a build that raised is already reported to ecFlow as
      ``aborted`` by the existing ``EcflowClient``/``Task.run`` machinery, so
      ``complete`` vs. anything else *is* the build's pass/fail outcome.
    - ``testbed`` (the default): success is reused, not reimplemented, from the
      case's own ``summary.json``, which
      ``tactus.reference_checker.ReferenceCheckManager``/``CheckSummaryAnalysis``
      already analyzed and wrote an ``analysis.success`` boolean into -- read
      directly here, independent of ``tactus test``'s own exit code.

    The status file is written to ``@CASEDIR@/ci_status.txt`` (`` system.casedir``
    resolved via ``Platform.get_system_value("casedir")``), which for the compile
    suite resolves under the compile suite's own case (e.g.
    ``.../IAL_<tag>_compile/ci_status.txt``) and for a testbed case resolves under
    that case's own scratch directory -- giving each context/case a distinct,
    predictable path without needing a separate macro.

    """

    def __init__(self, config):
        """Construct the CIMonitor task.

        Args:
            config (ParsedConfig): Configuration

        """
        Task.__init__(self, config, __class__.__name__)

        self.context = self.config.get("task.args.context", "testbed")
        self.build_path = self.config.get("task.args.build_path", None)
        self.case_name = self.platform.substitute(self.config.get("general.case"))
        self.status_file = None

    def _check_compile_success(self):
        """Determine PASS/FAIL for the compile context.

        Returns:
            tuple[bool, str]: success flag and a human-readable detail line.

        """
        if not self.build_path:
            return False, "No build_path was provided to CIMonitor; cannot verify build"

        server = EcflowServer(self.config)
        state = server.get_node_state(self.build_path)
        success = state is not None and str(state) == "complete"
        return success, f"TactusBundleBuild ({self.build_path}) ecFlow state: {state}"

    def _check_testbed_success(self):
        """Determine PASS/FAIL for the testbed context.

        Returns:
            tuple[bool, str]: success flag and a human-readable detail line.

        """
        json_file = self.platform.substitute(
            self.config.get("reference_checker.summary.json.file")
        )
        try:
            with open(json_file, "r", encoding="utf-8") as f:
                summary = json.load(f)
        except FileNotFoundError:
            return False, f"Reference checker summary not found: {json_file}"

        if "analysis" not in summary:
            return False, f"Reference checker summary has no analysis yet: {json_file}"

        analysis = summary["analysis"]
        return bool(analysis.get("success", False)), (
            f"Reference checker summary ({json_file}): {analysis.get('result')}"
        )

    def execute(self):
        """Evaluate pass/fail for this checkpoint and write ``ci_status.txt``."""
        if self.context == "compile":
            success, detail = self._check_compile_success()
        else:
            success, detail = self._check_testbed_success()

        self.write_status_file(success, detail)

    def write_status_file(self, success, detail):
        """Write the canonical CI status marker file.

        Args:
            success (bool): Whether the monitored context passed.
            detail (str): Human-readable detail of what was checked.

        Returns:
            str: The resolved path of the written ``ci_status.txt`` file.

        """
        casedir = self.platform.get_system_value("casedir")
        tactusmakedirs(casedir, unixgroup=self.unix_group)
        status_file = os.path.join(casedir, STATUS_FILENAME)

        result = "PASS" if success else "FAIL"
        lines = [
            f"Case: {self.case_name}",
            f"Context: {self.context}",
            f"Checked: {detail}",
            f"Timestamp: {datetime.now().isoformat(timespec='seconds')}",
            f"RESULT={result}",
        ]
        with open(status_file, "w", encoding="utf-8") as out:
            out.write("\n".join(lines) + "\n")

        logger.info("CIMonitor: wrote {} (RESULT={})", status_file, result)
        self.status_file = status_file
        return status_file
