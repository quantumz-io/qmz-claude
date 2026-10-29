#!/usr/bin/env bash
# Create a venv for the MCP server, with veloxq_sdk and mcp installed. run.sh
# uses it to provision the shared venv at ~/.veloxq/venv; it also builds
# scratch venvs (e.g. for a local SDK checkout under development).
#
# usage: setup_env.sh ENV_DIR [SDK_SOURCE]
#   ENV_DIR     directory for the venv (created if absent)
#   SDK_SOURCE  local checkout path or pip requirement
set -euo pipefail

ENV_DIR=${1:?usage: setup_env.sh ENV_DIR [SDK_SOURCE]}
SDK_SOURCE=${2:-veloxq-sdk}

if [ -d "$SDK_SOURCE" ] && [ ! -f "$SDK_SOURCE/pyproject.toml" ]; then
    echo "error: $SDK_SOURCE is a directory but has no pyproject.toml" >&2
    exit 1
fi

python3 -m venv "$ENV_DIR"
"$ENV_DIR/bin/pip" install --quiet --upgrade pip
"$ENV_DIR/bin/pip" install --quiet "$SDK_SOURCE" "mcp>=2.3,<3"

"$ENV_DIR/bin/python" - "$SDK_SOURCE" <<'EOF'
import sys
import dimod, mcp.server, veloxq_sdk
try:
    from veloxq_sdk.api.core.http import use_access_token
except ImportError:
    sys.exit(f"error: veloxq_sdk {veloxq_sdk.__version__} from {sys.argv[1]!r} has no "
             "per-request access tokens (use_access_token), which the MCP server needs. "
             "Set VELOXQ_SDK_SOURCE to a checkout or release that has them.")
print(f"veloxq_sdk {veloxq_sdk.__version__} ready "
      f"(dimod {dimod.__version__}, mcp installed)")
EOF
