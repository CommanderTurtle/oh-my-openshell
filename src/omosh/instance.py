# SPDX-FileCopyrightText: 2026 CommanderTurtle
# SPDX-License-Identifier: AGPL-3.0-only
"""One process owns each state directory, independently of its chosen HTTP port."""
from contextlib import contextmanager
import os
from pathlib import Path


@contextmanager
def instance_lock(root: Path):
    root.mkdir(parents=True, exist_ok=True, mode=0o700)
    file = (root / ".instance.lock").open("a+b")
    os.chmod(root / ".instance.lock", 0o600)
    try:
        if os.name == "posix":
            import fcntl
            fcntl.flock(file, fcntl.LOCK_EX | fcntl.LOCK_NB)
        else:
            import msvcrt
            if file.seek(0, 2) == 0:
                file.write(b"\0")
                file.flush()
            file.seek(0)
            msvcrt.locking(file.fileno(), msvcrt.LK_NBLCK, 1)
    except OSError as error:
        file.close()
        raise RuntimeError("This state directory already has a running omosh instance.") from error
    try:
        yield
    finally:
        file.close()
