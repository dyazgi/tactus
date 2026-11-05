# Shared bash helpers for self-hosted CI (adapted from the HARMONIE
# self-hosted CI's helpers.sh).

function log_msq(){
    echo "--MESSAGE--: " $@
}

# Keepalive: the monitor holds one ssh session open for hours.
function helper_ssh(){
    ssh -o ServerAliveInterval=60 -o ServerAliveCountMax=10 hpc-batch "$@"
}

function helper_mkfile(){
    local  output=$1
    helper_ssh "mkdir -p $(dirname $output); touch $output"
}

function helper_mkdir(){
    local  dir=$1
    helper_ssh "mkdir -p $dir"
}

function helper_exec(){
    local  cmd=$1
    helper_ssh "$cmd"
}

# helper_exec with python3/ecflow modules loaded.
function helper_ml_exec(){
    local  cmd=$1
    helper_ssh "module load python3/3.10.10-01; module load ecflow; $cmd"
}

# The runner has no python/module/$SCRATCH: run every poetry/python command
# through this. Runs "$@" on hpc-batch in the caller's $PWD, with the
# project .venv (once it exists) activated.
# SYSTEM_SITE_PACKAGES=false: jobs load other modules than install time
# (seen: ModuleNotFoundError marshmallow in a job after a green install).
function helper_py_exec(){
    local args
    printf -v args '%q ' "$@"
    helper_ml_exec "cd '$PWD' && export POETRY_VIRTUALENVS_OPTIONS_SYSTEM_SITE_PACKAGES=false && { [ ! -f .venv/bin/activate ] || source .venv/bin/activate; } && $args"
}

function helper_getVar(){
    local  varname=$1
    helper_ssh "echo \$$varname"
}

# Prints ial_tag+ial_git_version (at most one may be set); errors if both are.
function ci_resolve_ial_ref(){
    local  ial_tag=$1
    local  ial_git_version=$2
    if [ -n "$ial_tag" ] && [ -n "$ial_git_version" ]; then
        echo "::error::Only one of ial_tag / ial_git_version may be set, not both." >&2
        return 1
    fi
    echo "${ial_tag}${ial_git_version}"
}
