#!/usr/bin/env -S bash --login
set -euo pipefail
# This script is the one that is called by the DPS.
# Use this script to prepare input paths for any files
# that are downloaded by the DPS and outputs that are
# required to be persisted

# Get current location of build script
basedir=$(dirname "$(readlink -f "$0")")

# Create output directory to store outputs.
# The name is output as required by the DPS.
# Note how we dont provide an absolute path
# but instead a relative one as the DPS creates
# a temp working directory for our code.

mkdir -p output


# DPS downloads all files provided as inputs to
# this directory called input.
# In our example the image will be downloaded here.
INPUT_DIR=input
OUTPUT_DIR=output

# Call the script using the absolute paths
# Use the updated environment when calling 'uv run'
# This lets us run the same way in a Terminal as in DPS
# Any output written to the stdout and stderr streams will be automatically captured and placed in the output dir

# unset PROJ env vars
unset PROJ_LIB
unset PROJ_DATA

args=(
    --start_datetime "${1}"
    --end_datetime "${2}"
    --output_name "${5}"
    --output_dir "${OUTPUT_DIR}"
    --direct_bucket_access
)

[[ -n "${3}" ]] && args+=(--bbox ${3})
[[ -n "${4}" ]] && args+=(--crs "${4}")
[[ -n "${6}" ]] && args+=(--aoi "${6}")
[[ -n "${7}" ]] && args+=(--composite_type "${7}")
[[ -n "${8}" ]] && args+=(--q "${8}")
[[ -n "${9}" ]] && args+=(--lim "${9}")
[[ -n "${10}" ]] && args+=(--catalog)
[[ -n "${11}" ]] && args+=(--indices ${11})

export UV_PROJECT="${basedir}"

command=(uv run --no-dev ${basedir}/main.py "${args[@]}")
echo "${command[@]}"
"${command[@]}"
