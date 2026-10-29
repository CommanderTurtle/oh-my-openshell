# SPDX-FileCopyrightText: 2026 CommanderTurtle
# SPDX-License-Identifier: AGPL-3.0-only
"""PTY attachment with renderer acknowledgements providing backpressure."""
from __future__ import annotations

import asyncio
import os
import struct
import subprocess
import sys

from fastapi import WebSocket

from .process import terminate


async def attach(websocket: WebSocket, sessions, session):
    if os.name != "posix":
        await websocket.close(code=1008, reason="Terminal requires Linux, WSL2, or macOS.")
        return
    import fcntl
    import pty
    import termios
    master, slave = pty.openpty()
    process = None
    await websocket.accept()
    acknowledgement = asyncio.Event()
    acknowledgement.set()
    try:
        fcntl.ioctl(master, termios.TIOCSWINSZ, struct.pack("HHHH", 30, 100, 0, 0))
        process = await asyncio.create_subprocess_exec(sys.executable, "-m", "omosh.pty_child", "--", *sessions.terminal_argv(session),
                    stdin=slave, stdout=slave, stderr=slave, env={**sessions.runtime.env(), "TERM": "xterm-256color"}, start_new_session=True)
        os.close(slave)
        slave = -1

        async def output():
            while True:
                await asyncio.wait_for(acknowledgement.wait(), 60)
                acknowledgement.clear()
                try:
                    data = await asyncio.to_thread(os.read, master, 8192)
                except OSError:
                    return
                if not data:
                    return
                await websocket.send_bytes(data)

        async def input_():
            while True:
                message = await websocket.receive_json()
                if message.get("type") == "ack":
                    acknowledgement.set()
                elif message.get("type") == "input":
                    text = message.get("data", "")
                    if session["id"] in sessions.jobs.session_busy:
                        if text == "\x03":
                            for job_id in list(sessions.jobs.tasks):
                                if sessions.state.get("jobs", job_id, {}).get("session") == session["id"]:
                                    await sessions.jobs.cancel(job_id)
                        else:
                            await websocket.send_json({"type": "busy", "detail": "Agent command active. Ctrl-C cancels it; other input is paused."})
                        continue
                    if not isinstance(text, str) or len(text.encode()) > 16384:
                        raise ValueError("Input frame exceeds 16 KiB.")
                    data = text.encode()
                    while data:
                        written = await asyncio.to_thread(os.write, master, data)
                        data = data[written:]
                elif message.get("type") == "resize":
                    rows, cols = int(message["rows"]), int(message["cols"])
                    if not (2 <= rows <= 300 and 10 <= cols <= 500):
                        raise ValueError("Terminal dimensions out of range.")
                    fcntl.ioctl(master, termios.TIOCSWINSZ, struct.pack("HHHH", rows, cols, 0, 0))

        tasks = [asyncio.create_task(output()), asyncio.create_task(input_()), asyncio.create_task(process.wait())]
        try:
            await asyncio.wait(tasks, return_when=asyncio.FIRST_COMPLETED)
        finally:
            for task in tasks:
                task.cancel()
            await asyncio.gather(*tasks, return_exceptions=True)
    finally:
        os.close(master)
        if slave >= 0:
            os.close(slave)
        if process:
            await terminate(process)
        try:
            await websocket.close()
        except RuntimeError:
            pass
