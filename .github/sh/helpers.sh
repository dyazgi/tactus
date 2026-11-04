# Shared ssh-to-HPC helpers for self-hosted CI, adapted from the reference
# HARMONIE self-hosted CI
# (HarmonieCSC49T2h/.github/workflows/self-hosted/sh/helpers.sh). Trimmed to
# the functions actually used here - helper_follow/helper_ecflow_client are
# dropped since they wrap ecflow_client over ssh, while tactus's EcflowServer
# (tactus/scheduler.py) talks to ecFlow over its TCP client API instead and
# has no shell ecflow_client usage to route through ssh.

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

function helper_getVar(){
    local  varname=$1
    ssh hpc-batch "echo \$$varname"
}
