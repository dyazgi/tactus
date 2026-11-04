#!/bin/bash
# Force-remove every ecFlow suite whose name starts with a given prefix, via
# ecflow_client directly on this runner host - no python/poetry needed.
#
# Safety net for CI cleanup: `tactus remove` (see cleanup-tactus-run/
# action.yml) operates per-case-config file, and a crash/exception removing
# one case aborts only that one invocation - but if every case in a run hits
# the same bug, every one of them is left behind, and since each CI run's
# cases are named from a unique [general].tag prefix (see run-testbed's
# "Generate per-case configs" step), this repeats and accumulates run over
# run. This sweeps by suite *name prefix* instead, and isolates failures per
# suite so one broken suite can't block the rest - suitable both as an
# unconditional (if: always()) CI step and for clearing an already-
# accumulated backlog by hand.
#
# Host selection mirrors tactus's own SelectHost._select_host_from_list
# (tactus/host_actions.py) for atos_bologna (tactus/data/config_files/
# include/scheduler/ecflow_atos_bologna.toml): try each candidate host,
# first one that answers a ping wins. Port is fixed at 3141 there too.
# Scope note: this only removes the ecFlow suite nodes themselves (matching
# the confirmed working pattern below), not their ECF_HOME/ECF_OUT job-log
# directories on HPC scratch - unlike `tactus remove`, which does.
#
# Usage:
#   scripts/ci/sweep_suites.sh <suite-name-prefix>
#
# Example, to clear an already-accumulated backlog for a branch regardless
# of run id:
#   scripts/ci/sweep_suites.sh feature_ecmwf_self_hosted_runner_development__

set -uo pipefail

prefix="${1:?Usage: $0 <suite-name-prefix>}"

if ! command -v ecflow_client >/dev/null 2>&1; then
  echo "::error::ecflow_client not found on PATH on this runner host" >&2
  exit 1
fi

ecf_port="${ECF_PORT:-3141}"

if [ -n "${ECF_HOST:-}" ]; then
  ecf_host="$ECF_HOST"
else
  ecf_host=""
  for candidate in "ecfg-${USER}-1" "ecflow-gen-${USER}-001"; do
    if ping -c 1 "$candidate" >/dev/null 2>&1; then
      if [ -n "$ecf_host" ]; then
        echo "::error::Ambiguous host selection: both $ecf_host and $candidate responded" >&2
        exit 1
      fi
      ecf_host="$candidate"
    fi
  done
  if [ -z "$ecf_host" ]; then
    echo "::error::No ecFlow host found (tried ecfg-${USER}-1, ecflow-gen-${USER}-001)" >&2
    exit 1
  fi
fi

echo "Using ECF_HOST=$ecf_host ECF_PORT=$ecf_port"

mapfile -t suite_names < <(
  ecflow_client --host="$ecf_host" --port="$ecf_port" --get_state 2>/dev/null \
    | grep "^suite" | awk '{print $2}'
)

matching=()
for suite in "${suite_names[@]}"; do
  case "$suite" in
    "$prefix"*) matching+=("$suite") ;;
  esac
done

if [ "${#matching[@]}" -eq 0 ]; then
  echo "No suites found matching prefix '$prefix'"
  exit 0
fi

failures=0
for suite in "${matching[@]}"; do
  echo "Removing suite /$suite"
  if ! ecflow_client --host="$ecf_host" --port="$ecf_port" --delete=force yes "/$suite"; then
    echo "::warning::Failed to remove suite /$suite" >&2
    failures=$((failures + 1))
  fi
done

echo "Swept ${#matching[@]} suite(s) matching '$prefix' ($failures failure(s))"
[ "$failures" -eq 0 ]
