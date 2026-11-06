function log_msq(){
    echo "--MESSAGE--: " $@
}

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

function helper_ml_exec(){
    local  cmd=$1
    helper_ssh "module load python3/3.10.10-01; module load ecflow; $cmd"
}

# Runs "$@" on hpc-batch in $PWD with .venv activated.
function helper_py_exec(){
    local args
    printf -v args '%q ' "$@"
    helper_ml_exec "cd '$PWD' && { [ ! -f .venv/bin/activate ] || source .venv/bin/activate; } && $args"
}

function helper_getVar(){
    local  varname=$1
    helper_ssh "echo \$$varname"
}

function ci_resolve_ial_ref(){
    local  ial_tag=$1
    local  ial_git_version=$2
    if [ -n "$ial_tag" ] && [ -n "$ial_git_version" ]; then
        echo "::error::Only one of ial_tag / ial_git_version may be set, not both." >&2
        return 1
    fi
    echo "${ial_tag}${ial_git_version}"
}
