# /// script
# requires-python = ">=3.10"
# dependencies = ["dwave-cloud-client>=0.11,<1"]
# ///
"""Report whether a Leap API token is configured and which solvers it reaches.

Prints one JSON object. Exit status: 0 when solvers were listed, 1 when no
token is configured, 2 when the token is rejected, 3 on any other failure
(no network, a malformed configuration file, an endpoint outside Leap).
"""

import json
import os
import sys
from urllib.parse import urlparse

from dwave.cloud import Client
from dwave.cloud.config import load_config
from dwave.cloud.exceptions import SolverAuthenticationError
from ocean_config import active_profile, config_files, read_config, silence_debug_logging

LEAP_HOST_SUFFIX = ".dwavesys.com"


def token_source(paths):
    if os.environ.get("DWAVE_API_TOKEN"):
        return "environment variable DWAVE_API_TOKEN"
    parser = read_config(paths)
    profile = active_profile(parser)
    if not parser.has_option(profile, "token") or not parser.get(profile, "token"):
        return None
    holder = profile if profile in parser.sections() and "token" in parser[profile] else "defaults"
    files = [p for p in paths if os.path.exists(p)]
    return f"{', '.join(files)} [{holder}]"


def describe(solver):
    props = solver.properties
    out = {"name": solver.name, "category": props.get("category"), "online": solver.online}
    if solver.qpu:
        out["num_qubits"] = props.get("num_qubits")
    else:
        out["max_variables"] = props.get("maximum_number_of_variables")
    return out


def main():
    silence_debug_logging()
    paths = config_files()
    report = {"token_found": False, "source": None, "config_files": [p for p in paths if os.path.exists(p)],
              "endpoint": None, "solvers": [], "error": None}

    def finish(code, error=None):
        report["error"] = error
        print(json.dumps(report, indent=2))
        return code

    try:
        source = token_source(paths)
        config = load_config(config_file=paths)
    except Exception as err:
        return finish(3, f"Reading the Ocean configuration failed: {type(err).__name__}: {err}")
    report["token_found"] = source is not None
    report["source"] = source
    if source is None:
        return finish(1, "No Leap API token: neither DWAVE_API_TOKEN nor a dwave.conf token is set.")

    endpoint = config.get("endpoint")
    report["endpoint"] = endpoint
    if endpoint and not (urlparse(endpoint).hostname or "").endswith(LEAP_HOST_SUFFIX):
        return finish(3, f"The configured endpoint {endpoint} is not a Leap endpoint; the token was not sent.")

    try:
        with Client.from_config(config_file=paths) as client:
            report["solvers"] = [describe(s) for s in client.get_solvers()]
    except SolverAuthenticationError as err:
        return finish(2, f"The Leap API rejected the token: {err}")
    except Exception as err:
        return finish(3, f"Reaching Leap failed: {type(err).__name__}: {err}")
    return finish(0)


if __name__ == "__main__":
    sys.exit(main())
