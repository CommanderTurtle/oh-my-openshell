# SPDX-FileCopyrightText: 2026 CommanderTurtle
# SPDX-License-Identifier: AGPL-3.0-only
"""Bounded subprocesses. Host commands always use argv, never a shell."""
from __future__ import annotations

import asyncio
import os
import signal

MAX_OUTPUT = 2 * 1024 * 1024


async def terminate(process):
    if process.returncode is not None:
        return
    try:
        if os.name == "posix":
            os.killpg(process.pid, signal.SIGTERM)
        else:
            process.terminate()
        await asyncio.wait_for(process.wait(), 3)
    except (ProcessLookupError, asyncio.TimeoutError):
        if process.returncode is None:
            if os.name == "posix":
                os.killpg(process.pid, signal.SIGKILL)
            else:
                process.kill()
            await process.wait()


async def run(argv, *, env=None, stdin=None, timeout=60, check=True):
    process = await asyncio.create_subprocess_exec(
        *map(str, argv), env=env, stdin=asyncio.subprocess.PIPE if stdin is not None else asyncio.subprocess.DEVNULL,
        stdout=asyncio.subprocess.PIPE, stderr=asyncio.subprocess.PIPE,
        start_new_session=os.name == "posix",
    )
    truncated = False

    async def collect(stream):
        nonlocal truncated
        result = bytearray()
        while chunk := await stream.read(16384):
            room = MAX_OUTPUT - len(result)
            result.extend(chunk[:max(0, room)])
            truncated |= len(chunk) > room
        return result.decode("utf-8", errors="replace")

    async def communicate():
        async def send():
            if stdin is not None:
                try:
                    process.stdin.write(stdin.encode() if isinstance(stdin, str) else stdin)
                    await process.stdin.drain()
                except (BrokenPipeError, ConnectionResetError):
                    pass
                finally:
                    process.stdin.close()
        _, out, err = await asyncio.gather(send(), collect(process.stdout), collect(process.stderr))
        await process.wait()
        return dict(stdout=out, stderr=err, exit_code=process.returncode, truncated=truncated)

    try:
        result = await asyncio.wait_for(communicate(), timeout)
    except BaseException:
        await terminate(process)
        raise
    if check and result["exit_code"]:
        raise RuntimeError((result["stderr"] or result["stdout"] or f"Command exited {result['exit_code']}")[:12000])
    return result
