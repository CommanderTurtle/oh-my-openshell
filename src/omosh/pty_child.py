# SPDX-FileCopyrightText: 2026 CommanderTurtle
# SPDX-License-Identifier: AGPL-3.0-only
"""Acquire the inherited PTY in a fresh process, then replace it with the CLI.

This avoids preexec_fn in the multithreaded dashboard process.
"""
import os
import sys


def main():
    import fcntl
    import termios
    arguments = sys.argv[1:]
    if arguments[:1] == ["--"]:
        arguments = arguments[1:]
    if not arguments:
        raise SystemExit("Missing PTY command")
    fcntl.ioctl(0, termios.TIOCSCTTY, 0)
    os.execvpe(arguments[0], arguments, os.environ)


if __name__ == "__main__":
    main()
