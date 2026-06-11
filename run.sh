#!/usr/bin/env -S bash --login
set -euo pipefail

basedir=$(dirname "$(readlink -f "$0")")
mkdir -p output
INPUT_DIR=input
OUTPUT_DIR=output

# unset PROJ env vars
unset PROJ_LIB
unset PROJ_DATA

#args=(
#    --start_datetime "${1}"
#    --end_datetime "${2}"
#    --output_name "${5}"
#    --output_dir "${OUTPUT_DIR}"
#    --direct_bucket_access
#)
#
#[[ -n "${3}" ]] && args+=(--bbox ${3})
#[[ -n "${4}" ]] && args+=(--crs "${4}")
#[[ -n "${6}" ]] && args+=(--aoi "${6}")
#[[ -n "${7}" ]] && args+=(--composite_type "${7}")
#[[ -n "${8}" ]] && args+=(--q "${8}")
#[[ -n "${9}" ]] && args+=(--lim "${9}")
#[[ -n "${10}" ]] && args+=(--catalog)
#[[ -n "${11}" ]] && args+=(--indices ${11})

export UV_PROJECT="${basedir}"

#command=(uv run --no-dev ${basedir}/main.py "${args[@]}")
command=(uv run --no-dev ${basedir}/test-imports.py)
echo "${command[@]}"
"${command[@]}"
