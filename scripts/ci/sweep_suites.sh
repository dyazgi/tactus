#!/bin/bash
# Force-removes every ecFlow suite whose name starts with a given prefix
# (via ecflow_client, no python/poetry needed). Safety net for `tactus
# remove` leaving suites behind on crash. Only removes suite nodes, not
# their ECF_HOME/ECF_OUT job logs.
#
# Usage: scripts/ci/sweep_suites.sh <suite-name-prefix>
#        scripts/ci/sweep_suites.sh --cancel <suite-name>...
#   --cancel: suspend the named suites and kill their running jobs (no delete).

set -uo pipefail

cancel=0
if [ "${1:-}" = "--cancel" ]; then
  cancel=1
  shift
fi
prefix="${1:?Usage: $0 [--cancel] <suite-name-prefix | suite-name...>}"

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

if [ "$cancel" -eq 1 ]; then
  failures=0
  for suite in "$@"; do
    echo "Suspending and killing /$suite"
    ecflow_client --host="$ecf_host" --port="$ecf_port" --suspend "/$suite" || failures=$((failures + 1))
    ecflow_client --host="$ecf_host" --port="$ecf_port" --kill "/$suite" || failures=$((failures + 1))
  done
  echo "Cancelled $# suite(s) ($failures failure(s))"
  [ "$failures" -eq 0 ]
  exit $?
fi

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
