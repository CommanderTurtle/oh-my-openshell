# SPDX-FileCopyrightText: 2026 CommanderTurtle
# SPDX-License-Identifier: AGPL-3.0-only
"""Explicit, transactional upstream update staging. No idle registry polling."""
from __future__ import annotations

import asyncio
import hashlib
import json
import re

import httpx

from .config import REFERENCE, ensure_config, fields, set_field
from .process import run
from .state import private_write
from .reference_sync import download as sync_reference

API = "https://api.github.com/repos/NVIDIA/OpenShell"


async def latest():
    async with httpx.AsyncClient(timeout=30, follow_redirects=True) as client:
        response = await client.get(f"{API}/releases/latest", headers={"Accept": "application/vnd.github+json"})
        response.raise_for_status()
        release = response.json()
        tag = release["tag_name"]
        if release.get("prerelease") or not re.fullmatch(r"v\d+\.\d+\.\d+", tag):
            raise ValueError("The latest release is not a stable version tag.")
        commit = await client.get(f"{API}/commits/{tag}")
        commit.raise_for_status()
        pin = {"repository": "https://github.com/NVIDIA/OpenShell", "version": tag,
               "commit": commit.json()["sha"], "published": release["published_at"], "schema": 2,
               "images": {}, "cli": {}, "release_url": release["html_url"], "notes": release.get("body", "")}
        for key, suffix in [("Linux-x86_64", "x86_64-unknown-linux-musl"), ("Linux-aarch64", "aarch64-unknown-linux-musl"), ("Darwin-arm64", "aarch64-apple-darwin")]:
            filename = f"openshell-{suffix}.tar.gz"
            asset = next((a for a in release["assets"] if a["name"] == filename), None)
            if not asset or not re.fullmatch(r"sha256:[a-f0-9]{64}", asset.get("digest") or ""):
                raise ValueError(f"Verified release digest missing for {filename}.")
            pin["cli"][key] = {"asset": filename, "sha256": asset["digest"].split(":")[1]}
        for image in ("gateway", "supervisor", "sandbox"):
            auth = await client.get("https://ghcr.io/token", params={"service": "ghcr.io", "scope": f"repository:nvidia/openshell/{image}:pull"})
            auth.raise_for_status()
            manifest = await client.head(f"https://ghcr.io/v2/nvidia/openshell/{image}/manifests/{tag.lstrip('v')}",
                headers={"Authorization": f"Bearer {auth.json()['token']}", "Accept": "application/vnd.oci.image.index.v1+json, application/vnd.docker.distribution.manifest.list.v2+json"})
            manifest.raise_for_status()
            digest = manifest.headers.get("Docker-Content-Digest", "")
            if not re.fullmatch(r"sha256:[a-f0-9]{64}", digest):
                raise ValueError(f"Registry digest missing for {image}.")
            pin["images"][image] = f"ghcr.io/nvidia/openshell/{image}@{digest}"
        # Keep a concrete reference to candidate changes; runtime preflight checks
        # the real configuration against this exact candidate gateway image.
        return pin


async def stage(runtime, pin):
    pin = dict(pin)
    report = {"candidate": pin["commit"], "status": "reviewing", "breaking": [], "notes": []}
    runtime.state.delete("config", "staged_pin")
    runtime.state.put("config", "compatibility", report)
    if pin["version"] == runtime.pin["version"] and pin["commit"] == runtime.pin["commit"]:
        report.update(status="current", notes=["Installed release matches the latest stable release."])
        runtime.state.put("config", "compatibility", report)
        return {"version": pin["version"], "status": "Already on the latest stable release."}
    try:
        await runtime.install_cli(pin)
        cli = runtime.root / "bin" / pin["version"] / "openshell"
        help_ = await run([str(cli), "sandbox", "exec", "--help"], env=runtime.env(), timeout=10)
        required = ["--no-login-shell", "--no-tty", "--tty", "--timeout", "--workdir"]
        missing = [flag for flag in required if not re.search(r"(?m)^\s*(?:-[A-Za-z],\s+)?" + re.escape(flag) + r"\b", help_["stdout"])]
        if missing:
            report["breaking"].append("Required sandbox exec flags removed: " + ", ".join(missing))
            raise ValueError(report["breaking"][-1])
        report["notes"].append("Pinned CLI retains all five bridge exec flags.")
        text = ensure_config(runtime.state).read_text()
        await runtime.native_preflight(candidate_config(text, pin), pin)
        report["notes"].append("Candidate gateway accepts this draft with the candidate runtime image pins.")
        reference = await sync_reference(pin, runtime.root)
        old_root = runtime.root / "references" / runtime.pin["commit"]
        old_root = old_root if (old_root / "ready.json").is_file() else REFERENCE
        old = {f["path"]: f for f in json.loads((old_root / "config-fields.json").read_text())}
        candidate_root = runtime.root / "references" / pin["commit"]
        new = {f["path"]: f for f in json.loads((candidate_root / "config-fields.json").read_text())}
        configured = [f["path"] for f in fields(text)]
        changed = [p for p in old.keys() & new.keys() if old[p].get("structure") != new[p].get("structure") and any(c == p or c.startswith(p + ".") for c in configured)]
        if changed:
            report["notes"].append("Configured declarations changed and passed native preflight: " + ", ".join(sorted(changed)))
        report["notes"].append(f"Reference synchronized: {reference['pages']} pages, {reference['settings']} settings, {reference['commands']} command forms.")
        report["notes"].append("Existing sandboxes keep their runtime until recreated; extension services keep their independent version contracts.")
        pin["contracts"] = {"exec_flags": required, "native_preflight": "passed", "config_sha256": hashlib.sha256(text.encode()).hexdigest()}
        report["status"] = "ready"
        runtime.state.put("config", "staged_pin", pin)
    except BaseException as error:
        report["status"] = "blocked" if report["breaking"] else "interrupted" if isinstance(error, asyncio.CancelledError) else "error"
        report["error"] = "Staging cancelled." if isinstance(error, asyncio.CancelledError) else str(error)
        raise
    finally:
        runtime.state.put("config", "compatibility", report)
    return {"version": pin["version"], "preflight": "passed", "reference": reference, "release_notes": pin.get("notes", ""),
            "review": "Review release notes and driver/extension compatibility before switching. Running environments keep their existing runtime until recreated."}


def candidate_config(text, pin):
    for field, image in [("sandbox_runtime_image", "sandbox"), ("supervisor_image", "supervisor")]:
        text = set_field(text, f"openshell.drivers.docker.{field}", pin["images"][image])
    return text


async def apply(runtime, jobs):
    pin = runtime.state.get("config", "staged_pin")
    if not pin:
        raise ValueError("Stage a candidate first.")
    # This update itself is a running operator job; only other jobs prohibit it.
    if jobs.session_busy:
        raise ValueError("Wait for active bridge commands before switching versions.")
    path = ensure_config(runtime.state)
    previous_text = path.read_text()
    if pin.get("contracts", {}).get("config_sha256") != hashlib.sha256(previous_text.encode()).hexdigest():
        raise ValueError("Gateway draft changed since staging. Stage this candidate again before applying.")
    text = candidate_config(previous_text, pin)
    previous = runtime.pin
    await runtime.stop()
    backup = await runtime.backup_database()
    runtime.state.put("config", "previous_pin", previous)
    # Preserve operator overrides, updating only managed runtime image pins.
    private_write(runtime.root / "gateway.toml.before-update", previous_text)
    try:
        runtime.state.put("config", "pin", pin)
        private_write(path, text)
        result = await runtime.start()
        if result["state"] != "reachable":
            raise RuntimeError(result["detail"])
        runtime.state.delete("config", "staged_pin")
        return {**result, **backup}
    except BaseException as cause:
        stopped = True
        try:
            await asyncio.shield(run(runtime.compose_args("stop"), env=runtime.env(), timeout=60))
        except Exception as error:
            stopped = False
            stop_error = str(error)
        if not stopped:
            raise RuntimeError("Candidate did not authenticate and could not be stopped. Candidate pin retained to match the running deployment. Inspect the owned Compose project before retrying. " + stop_error)
        # Configuration and binaries can be rolled back. Gateway database migration
        # rollback is deliberately not attempted without an upstream migration contract.
        runtime.state.put("config", "pin", previous)
        private_write(path, previous_text)
        await asyncio.shield(runtime.prepare())
        if isinstance(cause, asyncio.CancelledError):
            raise
        raise RuntimeError("Candidate could not be activated. Previous configuration/pin restored; gateway remains stopped for inspection. Retain the gateway database before any manual downgrade. " + str(cause)) from cause
