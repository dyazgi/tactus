"""CI status marker task."""

import json
import os
from datetime import datetime

from ..logs import logger
from ..os_utils import tactusmakedirs
from ..scheduler import EcflowServer
from .base import Task

STATUS_FILENAME = "ci_status.txt"


class CIMonitor(Task):
    """Write ``ci_status.txt`` (RESULT=PASS/FAIL) for CI; never raises."""

    def __init__(self, config):
        """Construct the task."""
        Task.__init__(self, config, __class__.__name__)

        self.context = self.config.get("task.args.context", "testbed")
        self.build_path = self.config.get("task.args.build_path", None)
        self.case_name = self.platform.substitute(self.config.get("general.case"))
        self.status_file = None

    def _check_compile_success(self):
        """Return (success, detail) from the build task's ecFlow state."""
        if not self.build_path:
            return False, "No build_path was provided to CIMonitor; cannot verify build"

        server = EcflowServer(self.config)
        state = server.get_node_state(self.build_path)
        success = state is not None and str(state) == "complete"
        return success, f"TactusBundleBuild ({self.build_path}) ecFlow state: {state}"

    def _check_testbed_success(self):
        """Return (success, detail) from the reference checker summary."""
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
        """Evaluate and write ``ci_status.txt``."""
        if self.context == "compile":
            success, detail = self._check_compile_success()
        else:
            success, detail = self._check_testbed_success()

        self.write_status_file(success, detail)

    def write_status_file(self, success, detail):
        """Write ``ci_status.txt`` and return its path."""
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
