# Shared bash helpers for self-hosted CI. The ssh-to-HPC ones (helper_*,
# lowercase) are adapted from the reference HARMONIE self-hosted CI
# (HarmonieCSC49T2h/.github/workflows/self-hosted/sh/helpers.sh). Trimmed to
# the functions actually used here - helper_follow/helper_ecflow_client are
# dropped since they wrap ecflow_client over ssh, while tactus's EcflowServer
# (tactus/scheduler.py) talks to ecFlow over its TCP client API instead and
# has no shell ecflow_client usage to route through ssh. Other functions here
# (e.g. ci_resolve_ial_ref) are tactus-CI-specific, not ssh-related, but live
# here too since this is the one shared-bash-code location for this CI.

function log_msq(){
    echo "--MESSAGE--: " $@
}

function helper_mkfile(){
    local  output=$1
    ssh hpc-batch "mkdir -p $(dirname $output); touch $output"
}

function helper_mkdir(){
    local  dir=$1
    ssh hpc-batch "mkdir -p $dir"
}

function helper_exec(){
    local  cmd=$1
    ssh hpc-batch "$cmd"
}

function helper_ml_exec(){
    local  cmd=$1
    # Like helper_exec, but for commands that actually need python/ecflow
    # (e.g. poetry/tactus) - not every ssh'd command does, so don't pay the
    # module-load cost (and risk whatever it might print, e.g. module MOTD
    # messages) for ones that don't. Every HPC-submitted job already does
    # these same two `module load` calls (see any generated .job script)
    # before running anything that needs python or ecflow - do the same
    # here, so a poetry-created .venv (and anything else this touches) is
    # built against the same python/ecflow the rest of the pipeline uses.
    ssh hpc-batch "module load python3/3.10.10-01; module load ecflow; $cmd"
}

function helper_getVar(){
    local  varname=$1
    ssh hpc-batch "echo \$$varname"
}

function ci_resolve_ial_ref(){
    # Validates that at most one of ial_tag/ial_git_version is set (both
    # compile's and testbed's workflow_dispatch inputs), and prints their
    # concatenation to stdout - exactly one non-empty string, or "" if
    # neither is set. Errors (to stderr) and returns non-zero if both are
    # set - callers running under `set -e` should let that abort the step.
    local  ial_tag=$1
    local  ial_git_version=$2
    if [ -n "$ial_tag" ] && [ -n "$ial_git_version" ]; then
        echo "::error::Only one of ial_tag / ial_git_version may be set, not both." >&2
        return 1
    fi
    echo "${ial_tag}${ial_git_version}"
}
