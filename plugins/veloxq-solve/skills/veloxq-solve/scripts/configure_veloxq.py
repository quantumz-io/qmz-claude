#!/usr/bin/env python3
"""Detect or set up VeloxQ credentials in ~/.veloxq/config.py.

Run once at install time. Afterwards every session authenticates through
~/.veloxq/config.py — submit.py reads it by default, so no per-session token
handling is needed.

Detection order:
    1. ~/.veloxq/config.py already exists      -> nothing to do
    2. VELOX_TOKEN set in the environment      -> persist it to the config file
    3. token piped on stdin                    -> save
    4. interactive terminal                    -> masked TUI prompt

The token value is never printed and never returned on stdout — only where
it went, so the key cannot leak into logs or an AI conversation context.
"""
from __future__ import annotations

import os
import shutil
import sys
from contextlib import contextmanager
from pathlib import Path

if sys.platform == "win32":
    import ctypes
    import msvcrt

    def enable_cursor_escapes() -> None:
        """Turn on VT processing: conhost ignores cursor escapes without it."""
        kernel32 = ctypes.windll.kernel32
        handle = kernel32.GetStdHandle(-11)  # stdout
        mode = ctypes.c_uint32()
        if kernel32.GetConsoleMode(handle, ctypes.byref(mode)):
            kernel32.SetConsoleMode(handle, mode.value | 0x0004)

    @contextmanager
    def raw_keys():
        """Yield a reader returning one keypress at a time, echo off."""
        def read() -> str:
            ch = msvcrt.getwch()
            while ch in ("\x00", "\xe0"):  # function/arrow key: discard both chars
                msvcrt.getwch()
                ch = msvcrt.getwch()
            return ch
        yield read
else:
    import termios
    import tty

    def enable_cursor_escapes() -> None:
        pass

    @contextmanager
    def raw_keys():
        """Yield a reader returning one keypress at a time, echo off."""
        fd = sys.stdin.fileno()
        saved = termios.tcgetattr(fd)
        tty.setcbreak(fd)
        try:
            yield lambda: sys.stdin.read(1)
        finally:
            termios.tcsetattr(fd, termios.TCSADRAIN, saved)

DEFAULT_CONFIG = Path.home() / ".veloxq" / "config.py"
TITLE = "VeloxQ setup"
PROMPT = "Token"


def write_config(path: Path, token: str) -> None:
    path.parent.mkdir(parents=True, exist_ok=True)
    path.parent.chmod(0o700)
    escaped = token.replace("\\", "\\\\").replace('"', '\\"')
    path.write_text(
        "c = get_config()  # noqa\n"
        f'\nc.VeloxQAPIConfig.token = "{escaped}"\n'
    )
    path.chmod(0o600)


def protected_input(prefix: str, max_stars: int | None = None) -> str | None:
    """Read a line with star echo, showing at most max_stars stars. The
    prefix is printed only after echo is off, so nothing typed (or pasted)
    at it can be echoed back. Returns the stripped line, or None if
    cancelled."""
    token: list[str] = []
    shown = 0
    with raw_keys() as read_key:
        sys.stdout.write(prefix)
        sys.stdout.flush()
        while True:
            ch = read_key()
            if ch in ("\r", "\n"):
                print()
                return "".join(token).strip()
            if ch in ("\x1b", "\x03", "\x04", "\x1a", ""):  # Esc/Ctrl-C/Ctrl-D/Ctrl-Z/EOF
                print()
                return None
            if ch in ("\x7f", "\x08"):
                if token:
                    token.pop()
                    if shown > len(token):
                        sys.stdout.write("\b \b")
                        sys.stdout.flush()
                        shown -= 1
            elif ch >= " ":
                token.append(ch)
                if max_stars is None or shown < max_stars:
                    sys.stdout.write("*")
                    sys.stdout.flush()
                    shown += 1


def prompt() -> str | None:
    """Star-masked boxed prompt. Returns the token, or None if cancelled."""

    if not (sys.stdin.isatty() and sys.stdout.isatty()):
        raise RuntimeError("needs an interactive terminal")

    cols = shutil.get_terminal_size().columns
    box_w = min(max(len(PROMPT) + 6, 50), max(cols - 2, 20))
    inner = box_w - 2

    def row(text: str, align: str = "left") -> str:
        if align == "right":
            return "│" + text[:inner].rjust(inner) + "│"
        else:
            return "│" + text[:inner].ljust(inner) + "│"

    enable_cursor_escapes()
    print(f"┌─ {TITLE} " + "─" * (box_w - len(TITLE) - 5) + "┐")
    print(row(f" saves to {DEFAULT_CONFIG}"))
    print(row(""))
    print(row("Enter: save   Esc: cancel ", align="right"))
    print("└" + "─" * inner + "┘")
    entry = f" {PROMPT}: "
    try:
        token = protected_input(f"\x1b[3A\r\x1b[1C{entry}",
                                max_stars=max(inner - len(entry) - 1, 0))
    except KeyboardInterrupt:
        print()
        token = None
    print("\n")  # step below the bottom border
    return token or None


def main() -> int:
    if len(sys.argv) > 1:
        print(f"error: unrecognized arguments: {' '.join(sys.argv[1:])}",
              file=sys.stderr)
        return 2

    if DEFAULT_CONFIG.is_file():
        print(f"already configured: {DEFAULT_CONFIG}")
        return 0

    token = os.environ.get("VELOX_TOKEN", "").strip()
    if not token and not sys.stdin.isatty():
        token = sys.stdin.readline().strip()
        if not token:
            print("error: no credentials found (no ~/.veloxq/config.py, "
                  "VELOX_TOKEN unset) and nothing on stdin; run this in a "
                  "real terminal for interactive setup", file=sys.stderr)
            return 1
    if not token:
        token = prompt()
        if not token:
            print("setup cancelled; nothing written", file=sys.stderr)
            return 1

    write_config(DEFAULT_CONFIG, token)
    print(f"token saved to {DEFAULT_CONFIG} (mode 600)")
    return 0


if __name__ == "__main__":
    sys.exit(main())
