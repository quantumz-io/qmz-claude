#!/usr/bin/env bash
# Start the VeloxQ MCP server from the plugin's shared venv, creating the venv
# on first use. Claude Code runs this over stdio; only stderr may carry text.
# Arguments go to the server: `run.sh serve --public-url URL` starts the hosted
# server over HTTP.
set -euo pipefail

HERE=$(cd "$(dirname "${BASH_SOURCE[0]}")" && pwd)
VENV=${VELOXQ_VENV:-$HOME/.veloxq/venv}
PY="$VENV/bin/python"

# The server needs the SDK's per-request access tokens; a venv holding an
# older release is rebuilt, and setup_env.sh reports when the source lacks them.
ready() {
    "$PY" -c "import mcp.server; from veloxq_sdk.api.core.http import use_access_token" \
        2>/dev/null
}

if ! ready; then
    mkdir -p -m 700 "$HOME/.veloxq"
    exec 9>"$HOME/.veloxq/venv.lock"
    flock 9
    if ! ready; then
        echo "veloxq-mcp: setting up $VENV" >&2
        bash "$HERE/setup_env.sh" "$VENV" \
            "${VELOXQ_SDK_SOURCE:-veloxq-sdk}" >&2
    fi
    exec 9>&-
fi

# The project directory is the working directory; it must never shadow the
# server's imports. PYTHONSAFEPATH keeps it off sys.path on Python 3.11+.
export PYTHONPATH="$HERE${PYTHONPATH:+:$PYTHONPATH}"
export PYTHONSAFEPATH=1
exec "$PY" -m veloxq_mcp "$@"
