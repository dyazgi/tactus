# Shared bash helpers for self-hosted CI (adapted from the HARMONIE
# self-hosted CI's helpers.sh).

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

# Like helper_exec, but loads python3/ecflow first (same modules every
# HPC-submitted job loads) - use for poetry/tactus commands.
function helper_ml_exec(){
    local  cmd=$1
    ssh hpc-batch "module load python3/3.10.10-01; module load ecflow; $cmd"
}

function helper_getVar(){
    local  varname=$1
    ssh hpc-batch "echo \$$varname"
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
