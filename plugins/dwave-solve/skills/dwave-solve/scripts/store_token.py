# /// script
# requires-python = ">=3.10"
# dependencies = ["dwave-cloud-client>=0.11,<1"]
# ///
"""Store a Leap API token in Ocean's configuration file, or remove it.

The token is read from standard input and written as the `token` option of
the profile Ocean reads. Other lines of the file are kept as they are. The
file is replaced atomically and readable by the owner only. Prints one JSON
object with the path and profile written. Pass --forget to remove the stored
token instead.
"""

import argparse
import json
import os
import re
import sys
import tempfile

from ocean_config import active_profile, read_config, user_config_file

TOKEN_RE = re.compile(r"^[A-Za-z0-9._-]+$")
SECTION_RE = re.compile(r"^\s*\[([^\]]+)\]\s*$")
TOKEN_LINE_RE = re.compile(r"^\s*token\s*[=:]")


def edit_lines(lines, profile, token):
    """Set or remove (token None) the token line inside [profile]."""
    out, section, done = [], None, False
    for line in lines:
        match = SECTION_RE.match(line)
        if match:
            if section == profile and not done and token is not None:
                out.append(f"token = {token}\n")
                done = True
            section = match.group(1)
        elif section == profile and TOKEN_LINE_RE.match(line):
            if token is not None and not done:
                out.append(f"token = {token}\n")
            done = True
            continue
        out.append(line)
    if not done and token is not None:
        if section != profile:
            if out and not out[-1].endswith("\n"):
                out[-1] += "\n"
            out.append(f"[{profile}]\n")
        out.append(f"token = {token}\n")
        done = True
    return out, done


def main():
    parser = argparse.ArgumentParser(description=__doc__,
                                     formatter_class=argparse.RawDescriptionHelpFormatter)
    parser.add_argument("--forget", action="store_true", help="remove the stored token")
    args = parser.parse_args()

    path = os.path.abspath(user_config_file())
    if os.path.islink(path):
        sys.exit(f"{path} is a symbolic link; edit its target yourself.")
    profile = active_profile(read_config([path]))

    if args.forget:
        token = None
    else:
        token = sys.stdin.read().strip()
        if not TOKEN_RE.match(token):
            sys.exit("Expected the token on standard input: one line of letters, digits, '.', '_' or '-'.")

    lines = open(path).readlines() if os.path.exists(path) else []
    new_lines, done = edit_lines(lines, profile, token)
    if not done:
        print(json.dumps({"path": path, "profile": profile, "removed": False}))
        return 1

    directory = os.path.dirname(path)
    os.makedirs(directory, exist_ok=True)
    fd, tmp = tempfile.mkstemp(prefix=".dwave.conf.", dir=directory)
    with os.fdopen(fd, "w") as fp:
        fp.writelines(new_lines)
    os.chmod(tmp, 0o600)
    os.replace(tmp, path)
    print(json.dumps({"path": path, "profile": profile, "removed" if args.forget else "stored": True}))
    return 0


if __name__ == "__main__":
    sys.exit(main())
