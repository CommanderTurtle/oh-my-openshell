# SPDX-FileCopyrightText: 2026 CommanderTurtle
# SPDX-License-Identifier: AGPL-3.0-only
from __future__ import annotations

import asyncio
import hashlib
import io
import json
import os
from pathlib import Path
import platform
import re
import shutil
import sqlite3
import tarfile
import tempfile
import time

import httpx

from .config import compose, ensure_config, load_pin, validate_config
from .process import run
from .process import terminate
from .state import private_write

NAME = re.compile(r"[a-z0-9](?:[a-z0-9-]{0,30}[a-z0-9])?\Z")


def name(value):
    if not isinstance(value, str) or not NAME.fullmatch(value):
        raise ValueError("Use 1–32 lowercase letters, numbers, or hyphens; start and end with a letter or number.")
    return value


class Runtime:
    def __init__(self, state):
        self.state = state
        self.lock = asyncio.Lock()
        self.root = state.root
        self.project = "omosh-" + hashlib.sha256(str(self.root).encode()).hexdigest()[:10]
        self.status = {"state": "unchecked", "detail": "Run the prerequisite check to inspect this host."}
        ensure_config(state)

    @property
    def pin(self):
        return self.state.get("config", "pin", load_pin())

    @property
    def cli_path(self):
        return self.root / "bin" / self.pin["version"] / "openshell"

    def env(self):
        # Ambient gateway credentials, endpoints, runtime overrides, and Docker contexts
        # must not silently change the managed installation.
        env = {k: v for k, v in os.environ.items() if not k.startswith(("OPENSHELL_", "DOCKER_", "COMPOSE_"))}
        env.update(XDG_CONFIG_HOME=str(self.root / "client"), NO_COLOR="1", OPENSHELL_TELEMETRY_ENABLED="false")
        return env

    def cli(self, args, workspace="default"):
        if not self.cli_path.is_file():
            raise RuntimeError("Install the pinned OpenShell CLI from Setup first.")
        return [str(self.cli_path), "--gateway", "omosh", "--workspace", name(workspace), "--color", "never", *args]

    async def call(self, args, workspace="default", **kwargs):
        return await run(self.cli(args, workspace), env=self.env(), **kwargs)

    async def exec(self, environment, command, *, stdin=None, timeout=30, check=True):
        return await self.call(["sandbox", "exec", environment["name"], "--no-login-shell", "--no-tty",
                                "--timeout", str(max(1, int(timeout))), "--", *command],
                               environment["workspace"], stdin=stdin, timeout=timeout + 5, check=check)

    async def verify_environment(self, environment):
        result = await self.call(["sandbox", "get", environment["name"], "--output", "json"], environment["workspace"], timeout=20)
        snapshot = json.loads(result["stdout"])
        if snapshot.get("id") != environment.get("sandbox_id") or snapshot.get("labels", {}).get("omosh.owner") != self.project:
            raise RuntimeError("Sandbox ownership changed. Inspect this environment before accessing or stopping it.")
        return snapshot

    def require_host(self):
        if os.name != "posix":
            raise RuntimeError("Run omosh inside Linux, WSL2, or macOS for Docker and PTY access. Native Windows supports dashboard inspection only.")
        if not shutil.which("docker"):
            raise RuntimeError("Docker is missing. Install Docker Engine 28+ and its Compose plugin, then run Check again.")

    def setup_state(self):
        doctor = self.state.get("config", "doctor", [])
        prerequisites = [item for item in doctor if item["name"] in ("Host", "Docker daemon", "Compose")]
        host = len(prerequisites) == 3 and all(item["ok"] is True for item in prerequisites)
        return {"host": host, "cli": self.cli_path.is_file(),
                "workload": self.state.get("config", "workload_built", False),
                "gateway": self.status["state"] == "reachable"}

    def docker(self, *args):
        self.require_host()
        return [shutil.which("docker"), "--host", "unix:///var/run/docker.sock", *args]

    def compose_args(self, *args):
        return self.docker("compose", "--project-name", self.project, "--file", str(self.root / "compose.yaml"), *args)

    async def doctor(self):
        checks = [{"name": "Host", "ok": os.name == "posix", "detail": f"{platform.system()} {platform.machine()} · runtime requires a POSIX host"}]
        for title, args in [("Docker daemon", ["info", "--format", "{{json .}}"]), ("Compose", ["compose", "version", "--short"])]:
            try:
                result = await run(self.docker(*args), env=self.env(), timeout=15)
                detail = result["stdout"].strip()
                if title == "Docker daemon":
                    info = json.loads(detail)
                    detail = f"Docker {info.get('ServerVersion', '?')} · {info.get('OSType', '?')} · {info.get('Architecture', '?')}"
                    if int(info.get("ServerVersion", "0").split(".")[0]) < 28:
                        raise RuntimeError("Docker Engine 28 or later is required.")
                    if info.get("OSType") != "linux":
                        raise RuntimeError("Switch Docker to Linux containers.")
                checks.append({"name": title, "ok": True, "detail": detail[:1000]})
            except Exception as error:
                checks.append({"name": title, "ok": False, "detail": str(error)})
        checks.append({"name": "OpenShell CLI", "ok": self.cli_path.is_file(), "detail": f"Pinned {self.pin['version']}; installed in omosh's private state directory."})
        checks.append({"name": "Kernel isolation", "ok": None, "detail": "OpenShell performs Landlock ABI v3, seccomp, and task-memory probes at runtime. Kernel version alone does not establish support."})
        self.state.put("config", "doctor", checks)
        return checks

    async def install_cli(self, pin=None):
        pin = pin or self.pin
        system = f"{platform.system()}-{platform.machine().lower()}"
        system = system.replace("amd64", "x86_64").replace("Linux-arm64", "Linux-aarch64")
        asset = pin["cli"].get(system)
        if not asset:
            raise RuntimeError(f"Upstream has no supported CLI archive for {system}. Use a Linux/WSL2 or Apple Silicon host.")
        url = f"https://github.com/NVIDIA/OpenShell/releases/download/{pin['version']}/{asset['asset']}"
        async with httpx.AsyncClient(follow_redirects=True, timeout=90) as client:
            async with client.stream("GET", url) as response:
                response.raise_for_status()
                archive = bytearray()
                async for chunk in response.aiter_bytes():
                    archive.extend(chunk)
                    if len(archive) > 64 * 1024 * 1024:
                        raise RuntimeError("CLI archive exceeds download limit.")
        if hashlib.sha256(archive).hexdigest() != asset["sha256"]:
            raise RuntimeError("CLI checksum mismatch. Existing binary preserved.")
        with tarfile.open(fileobj=io.BytesIO(archive), mode="r:gz") as tar:
            members = [m for m in tar.getmembers() if m.isfile() and Path(m.name).name == "openshell"]
            if len(members) != 1 or members[0].size > 128 * 1024 * 1024:
                raise RuntimeError("Unexpected CLI archive layout.")
            binary = tar.extractfile(members[0]).read()
        dest = self.root / "bin" / pin["version"] / "openshell"
        private_write(dest, binary)
        os.chmod(dest, 0o700)
        version = await run([str(dest), "--version"], env=self.env(), timeout=10)
        if pin["version"].lstrip("v") not in version["stdout"]:
            raise RuntimeError("Downloaded CLI reports an unexpected version.")
        return {"version": version["stdout"].strip(), "path": str(dest)}

    async def native_preflight(self, text, pin=None):
        validation = validate_config(text)
        if validation["errors"]:
            raise ValueError("\n".join(validation["errors"]))
        path = self.root / "candidate.toml"
        private_write(path, text)
        return await run(self.docker("run", "--rm", "--network", "none",
                                     "--mount", f"type=bind,src={path},dst=/etc/openshell/candidate.toml,readonly",
                                     (pin or self.pin)["images"]["gateway"], "config", "preflight", "--path", "/etc/openshell/candidate.toml"),
                         env=self.env(), timeout=120)

    async def prepare(self):
        text = ensure_config(self.state).read_text()
        report = validate_config(text)
        if report["errors"]:
            raise ValueError("\n".join(report["errors"]))
        (self.root / "gateway").mkdir(exist_ok=True, mode=0o700)
        existing = self.root / "compose.yaml"
        if existing.exists():
            import yaml
            previous = yaml.safe_load(existing.read_text())
            if not isinstance(previous, dict) or previous.get("services", {}).get("gateway", {}).get("labels", {}).get("io.omosh.managed") != "true":
                raise RuntimeError("compose.yaml already exists and is not marked as omosh-owned. Choose a separate state directory.")
        private_write(self.root / "compose.yaml", compose(self.root, text, self.pin, self.state.get("config", "telemetry", False)))
        return text

    async def start(self):
        async with self.lock:
            if not self.cli_path.is_file():
                raise RuntimeError("Install the pinned OpenShell CLI before starting the gateway.")
            await self.prepare()
            await self.native_preflight(ensure_config(self.state).read_text())
            await run(self.compose_args("up", "--detach", "--force-recreate"), env=self.env(), timeout=180)
            # The root-run initializer owns its files. Copy only the client bundle
            # from its stopped container, leaving the gateway's PKI permissions intact.
            data = (self.root / "gateway").as_posix()
            container = (await run(self.compose_args("ps", "--all", "--quiet", "init"), env=self.env(), timeout=10))["stdout"].strip()
            if not re.fullmatch(r"[a-f0-9]{12,64}", container):
                raise RuntimeError("The owned certificate initializer container could not be identified.")
            for filename, source in [("ca.crt", "ca.crt"), ("tls.crt", "client/tls.crt"), ("tls.key", "client/tls.key")]:
                destination = self.root / "client/openshell/gateways/omosh/mtls" / filename
                destination.parent.mkdir(parents=True, exist_ok=True, mode=0o700)
                await run(self.docker("cp", f"{container}:{data}/tls/{source}", str(destination)), env=self.env(), timeout=20)
                os.chmod(destination, 0o600)
            port = report_port(ensure_config(self.state).read_text())
            registration = self.root / "client/openshell/gateways/omosh/metadata.json"
            endpoint = f"https://127.0.0.1:{port}"
            if registration.exists():
                metadata = json.loads(registration.read_text())
                if metadata.get("gateway_endpoint") != endpoint:
                    bundle = {f: (registration.parent / "mtls" / f).read_bytes() for f in ("ca.crt", "tls.crt", "tls.key")}
                    await run([str(self.cli_path), "gateway", "remove", "omosh"], env=self.env(), timeout=20)
                    for f, data_ in bundle.items():
                        private_write(registration.parent / "mtls" / f, data_)
            if not registration.exists():
                await run([str(self.cli_path), "gateway", "add", endpoint, "--local", "--name", "omosh"], env=self.env(), timeout=20)
            # Workload build is explicit in Setup; a gateway can start without it.
            for attempt in range(8):
                result = await self.refresh()
                if result["state"] == "reachable":
                    return result
                await asyncio.sleep(min(2, .25 * (attempt + 1)))
            raise RuntimeError("Gateway started but did not authenticate within the startup deadline. Inspect gateway logs in the CLI panel. " + result["detail"])

    async def stop(self):
        async with self.lock:
            if not (self.root / "compose.yaml").exists():
                return {"state": "stopped"}
            # Stop each owned sandbox first. Compose stop alone leaves siblings alive.
            results = []
            for environment in self.state.list("environments"):
                snapshot = await self.verify_environment(environment)
                if snapshot.get("phase", "").lower() == "stopped":
                    continue
                results.append(await self.call(["sandbox", "stop", environment["name"]], environment["workspace"], check=False, timeout=30))
            if any(r["exit_code"] for r in results):
                raise RuntimeError("Some sandboxes could not stop. Gateway kept running; inspect their state before retrying.")
            await run(self.compose_args("stop"), env=self.env(), timeout=60)
            self.status = {"state": "stopped", "detail": "Owned gateway and sandboxes stopped. Data retained."}
            return self.status

    async def refresh(self):
        try:
            result = await self.call(["status", "--output", "json"], timeout=15)
            status = json.loads(result["stdout"])
            authenticated = status.get("authentication", {}).get("status") == "authenticated"
            self.status = {"state": "reachable" if authenticated else "unauthenticated", "detail": result["stdout"], "checked": __import__('time').time()}
        except Exception as error:
            self.status = {"state": "unavailable", "detail": str(error)}
        return self.status

    async def observe(self, publish):
        """Docker sends gateway lifecycle events while an operator view is open."""
        process = None
        try:
            argv = self.docker("events", "--filter", f"label=com.docker.compose.project={self.project}",
                               "--filter", "label=com.docker.compose.service=gateway", "--format", "{{json .}}")
            process = await asyncio.create_subprocess_exec(*argv, env=self.env(), stdin=asyncio.subprocess.DEVNULL,
                stdout=asyncio.subprocess.PIPE, stderr=asyncio.subprocess.DEVNULL, start_new_session=True)
            while line := await process.stdout.readline():
                event = json.loads(line)
                action = event.get("Action", event.get("status", ""))
                if action in ("die", "stop", "destroy"):
                    self.status = {"state": "stopped", "detail": "Docker reported that the owned gateway stopped.", "checked": time.time()}
                elif action == "start":
                    await self.refresh()
                else:
                    continue
                await publish("gateway", self.status)
        except asyncio.CancelledError:
            raise
        except (RuntimeError, FileNotFoundError, OSError, ValueError):
            # Manual refresh remains available. Avoid retries against a missing daemon.
            return
        finally:
            if process:
                await terminate(process)

    async def build_workload(self):
        # Build on the host; sandbox execution never receives the Docker socket.
        dockerfile = Path(__file__).parent / "workload/Dockerfile"
        result = await run(self.docker("build", "--label", "io.omosh.managed=true", "-t", "omosh-workload:local", str(dockerfile.parent)),
                           env=self.env(), timeout=600)
        self.state.put("config", "workload_built", True)
        return result

    async def backup_database(self):
        """Back up an offline gateway DB, including any WAL left after shutdown."""
        container = (await run(self.compose_args("ps", "--all", "--quiet", "gateway"), env=self.env(), timeout=10))["stdout"].strip()
        if not container:
            return {"backup": None, "detail": "Gateway has no existing container/database."}
        if not re.fullmatch(r"[a-f0-9]{12,64}", container):
            raise RuntimeError("Cannot identify the owned gateway container.")
        running = await run(self.docker("inspect", "--format", "{{.State.Running}}", container), env=self.env(), timeout=10)
        if running["stdout"].strip() != "false":
            raise RuntimeError("Stop the gateway before taking an offline database backup.")
        directory = self.root / "backups" / str(time.time_ns())
        directory.mkdir(parents=True, mode=0o700)
        with tempfile.TemporaryDirectory(prefix="sqlite-", dir=directory) as temporary:
            staging = Path(temporary)
            source = (self.root / "gateway/gateway.db").as_posix()
            for suffix in ("", "-wal", "-shm"):
                result = await run(self.docker("cp", f"{container}:{source}{suffix}", str(staging / f"gateway.db{suffix}")),
                                   env=self.env(), timeout=30, check=False)
                if result["exit_code"] and not suffix:
                    raise RuntimeError("Could not read the offline gateway database; update stopped before changing versions.")
            destination = directory / "gateway.sqlite3"
            def backup():
                with sqlite3.connect(staging / "gateway.db") as original, sqlite3.connect(destination) as saved:
                    original.backup(saved)
                    if saved.execute("PRAGMA quick_check").fetchone()[0] != "ok":
                        raise RuntimeError("Gateway database backup failed integrity verification.")
            await asyncio.to_thread(backup)
            os.chmod(destination, 0o600)
        private_write(directory / "pin.json", json.dumps(self.pin, indent=2))
        return {"backup": str(destination), "detail": "Verified SQLite backup; credentials remain encrypted by the original gateway key material."}


def report_port(text):
    import tomllib
    return int(tomllib.loads(text)["openshell"]["gateway"]["bind_address"].rsplit(":", 1)[1])
