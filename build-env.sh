#!/usr/bin/env -S bash --login
set -euo pipefail
# This script is used to install any custom packages required by the algorithm.

# install curl if not available
command -v curl >/dev/null 2>&1 || { 
    echo "Installing curl..."
    conda install curl
}

# install uv
curl -LsSf https://astral.sh/uv/install.sh | sh
source $HOME/.local/bin/env

# Get current location of build script
basedir=$( cd "$(dirname "$0")" ; pwd -P )

# install dependencies
UV_PROJECT=$basedir uv sync --no-dev
UV_PROJECT=$basedir uv pip install gdal

# unset PROJ env vars
unset PROJ_LIB
unset PROJ_DATA
