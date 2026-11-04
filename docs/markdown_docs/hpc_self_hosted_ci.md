# Self-hosted ECMWF CI (compile + testbed)

Two GitHub Actions workflows run tactus's compile step and its integration
test suite (the "testbed") on a self-hosted runner with real access to
ECMWF's Atos Bologna HPC:

 - `.github/workflows/tactus-compile-self-hosted-ecmwf.yml`
 - `.github/workflows/tactus-testbed-self-hosted-ecmwf.yml`

They wrap the exact commands documented in [Compiling](compiling.md) and
[Tactus test-runner](test_runner.md) (`tactus compile`, `tactus test`) —
there is no separate CI-only build/test path, only automation around what a
human already runs manually today.

This automates what was previously a fully manual step: testing on real
Atos hardware via the companion
[Tactus-test-runner](https://github.com/destination-earth-digital-twins/Tactus-test-runner)
repo and a checkbox in the [PR template](../../.github/pull_request_template.md).
The manual path still exists and is not required to be dropped until the
automated one has a track record (see "Rollout status" below).

## Triggering

Both workflows are triggered the same way:

 - **`workflow_dispatch`** — manual run from the GitHub Actions UI, with
   explicit inputs (`ial_tag`/`dry_run` for compile, `test_config`/
   `case_selection` for testbed).
 - **`push`**, gated by a magic string in the commit message:
   `--action-compile` for the compile workflow, `--action-testbed` for the
   testbed workflow. A push without that string in its commit message still
   triggers the workflow file, but the job itself is skipped (`if:` check).

This mirrors the convention used by the reference HARMONIE self-hosted CI
(`HarmonieCSC49T2h/.github`) on purpose, including one of its quirks: the
`if:` condition also checks `github.event.pull_request.title`/`.body` for
the magic string, but neither workflow is wired to the `pull_request` event,
so those two checks are always false in practice. They're kept for parity
and in case a `pull_request` trigger is added later — they are not currently
a way to trigger a run from a PR title/body.

Example: a commit message of `Fix boundary handling --action-testbed` will
run the testbed workflow on push.

## Required GitHub secrets & variables

Set these under **Settings → Secrets and variables → Actions** before
enabling the push-triggered path:

| Name | Kind | Required | Purpose |
|---|---|---|---|
| `GH_REPO_READ_TOKEN` | Secret | Yes (already exists) | Same token already used by `setup-github-credentials` for Poetry's private git dependencies (e.g. `troika`); reused here for checkout, no new secret needed. |
| `TACTUS_WLM_ACCOUNT` | Variable | Yes | SLURM/workload-manager account string for CI-submitted jobs (`#SBATCH --account=`). Fed into the `ci_atos_bologna.toml` submission overlay — see below. |
| `TACTUS_WLM_QOS` | Variable | Yes | SLURM QOS for CI-submitted jobs, if it must differ from `atos_bologna.toml`'s existing default (`np`). |
| `TACTUS_TEST_CONFIG` | Variable | No | Default test-suite config path if the `workflow_dispatch` input is left blank. Defaults to `tactus/data/tests/atos_bologna_CY50t2.toml` if unset. |
| `TACTUS_TESTBED_CASE_SELECTION` | Variable | No | Comma-separated case names to restrict a push-triggered testbed run to. Empty = every case in `TACTUS_TEST_CONFIG`. |
| `TACTUS_POLL_INTERVAL_SECONDS` | Variable | No | How often `scripts/ci/monitor_suite.py` polls suite state. Defaults to 60. |
| `TACTUS_SUITE_TIMEOUT_MINUTES` | Variable | No | Hard timeout for a run before it's suspended and marked `TIMEOUT`. Defaults to 180 (compile) / 240 (testbed). |

`TACTUS_WLM_ACCOUNT`/`TACTUS_WLM_QOS` are not just passed through the
workflow — they're substituted into a real submission config key (see next
section), so a value is never silently dropped the way the reference repo's
equivalent (`submit_account`) was.

## SLURM account/QOS config, and the IAL repo override

`tactus/data/config_files/modifications/ci_atos_bologna.toml` layers
`#SBATCH --account=`/`--qos=` onto the submission settings that submit real
SLURM jobs, substituting the `TACTUS_WLM_ACCOUNT`/`TACTUS_WLM_QOS`
environment variables (exported from the `vars.*` above) via the same
`@VAR@`/`os_macros` mechanism already used elsewhere in this config tree.
This file is only applied for CI runs; interactive use of `atos_bologna` is
unaffected.

The same file also overrides `compile.ial_git_repo` to
`https://github.com/Hirlam/IAL` — ECMWF's self-hosted CI must build from the
Hirlam fork, not the ACCORD-NWP upstream repo that `config.toml` defaults to
everywhere else. See [Compiling](compiling.md#required-configuration).

**Compile**: fully wired up — `run-compile` passes this file as a positional
`extra_config_mods` argument to `tactus compile` (a small CLI addition; see
`tactus/argparse_wrapper.py`'s `parser_compile`), which appends it after the
command's own `@HOST@.toml`/`compile_suite.toml` modifications.

**Testbed — known gap**: `tactus test`'s per-case submission settings come
from the test config's own `[include].modifs` file (e.g.
`modifs_atos_bologna_CY50t2.toml`), not from a CLI-passed modification file,
and that file has no CI account/QOS override wired in yet. Until it does,
real testbed SLURM jobs submitted by `tactus-testbed-self-hosted-ecmwf.yml`
use whatever account/QOS `modifs_atos_bologna_CY50t2.toml` and
`submission/atos_bologna.toml` already resolve to for interactive use — set
`TACTUS_WLM_ACCOUNT`/`TACTUS_WLM_QOS` regardless (compile needs them), but
don't assume they affect testbed job submission yet. A follow-up should add
the equivalent `account`/`QOS` keys (same `@TACTUS_WLM_ACCOUNT@`/
`@TACTUS_WLM_QOS@` substitution) to a CI-specific modifs overlay, without
touching the interactive test config (adding an unconditional `@VAR@`
reference to the shared, interactively-used `modifs_atos_bologna_CY50t2.toml`
would break manual runs that don't have these environment variables set).

## The Monitor mechanism

Rather than only polling `summary.json` at an interval, a `CIMonitor` ecFlow
task is appended as the last task of the compile suite (after
`TactusBundleBuild`) and of each testbed case (after the existing reference
check). It writes a small status file ending in a `RESULT=PASS` or
`RESULT=FAIL` line once its family/case reaches a terminal state — this is
tactus's equivalent of the reference repo's `ci_stream.log`/`ci_status.txt`
convention.

`scripts/ci/monitor_suite.py` (run by the `run-compile`/`run-testbed`
composite actions right after the suite is submitted) polls each
`CIMonitor` task's ecFlow state, streams its job output into the GitHub
Actions log as it becomes available, and once the status file's `RESULT=`
line appears, writes a markdown summary to the workflow's step summary and
exits 0 (PASS) or 1 (FAIL) — this is what makes the GitHub check go red or
green. On timeout or cancellation it suspends the still-running suite first,
so a stuck run doesn't keep burning HPC allocation unattended, then reports
`TIMEOUT` rather than `FAIL` so the difference is visible in the summary.

## Cleanup & persistent state

`cleanup-tactus-run` (a composite action, run with `if: always()` at the end
of both workflows) removes the ecFlow suite(s) for the run and the ephemeral
per-run config directory on the runner, via tactus's own
`tactus remove --execute-removal --force-remove` — the same command
documented in [Tactus test-runner](test_runner.md), not a hand-rolled
`rm -rf` of guessed paths. `--force-remove` also removes suites that never
reached `complete` (a cancelled or timed-out run), so orphaned SLURM jobs
don't linger.

**Kept on purpose, across runs** (never touched by cleanup):
 - The compile build cache under `[platform_paths.install_dir]`
   (`/perm/deployde330/github-actions/install/` for `atos_bologna`), reused
   by later compiles/testbed runs.
 - The reference-experiment tree used by the reference checker.

**Always removed, regardless of outcome:**
 - The ecFlow suite(s) submitted for the run.
 - The generated `compile_<tag>.toml` / `<tag>_configs/` workspace checked
   out on the runner.

## Runbook: a stuck or failed run

 - **Inspect**: open the uploaded `tactus-testbed-<run-id>` artifact (case
   `summary.json`, `.diff` files, `ci_status.txt`) from the failed workflow
   run, or re-read `$GITHUB_STEP_SUMMARY` from the run page.
 - **A suite is stuck/orphaned on Atos**: use the standard `tactus remove`
   command against the case's config (see [Tactus
   test-runner](test_runner.md#remove-the-tests-from-disk-and-ecflow)), or
   inspect/suspend it directly with `ecflow_client` if you need to look at
   it before removing it.
 - **Re-run a single failed case locally**: `tactus test -c <config> -l` to
   confirm the case name, then restrict a manual `workflow_dispatch` run's
   `case_selection` input to just that case, or run `tactus test` directly
   on the ecFlow VM outside of CI.

## Rollout status

Both workflows are triggered the same way as the reference HARMONIE
self-hosted CI (push + magic commit-message string, or manual dispatch) —
there is no separate "staged" trigger. Before relying on the push-triggered
path day to day, validate manually via `workflow_dispatch`:

1. `tactus compile -d` (dry run, no HPC cost) via the compile workflow's
   `dry_run` input.
2. One real compile.
3. One testbed run with `case_selection` restricted to a single, cheap case.
4. The full default `TACTUS_TEST_CONFIG` selection.

Keep this section up to date with whichever triggers are actually relied on
for `develop`/`master` as the rollout progresses.
