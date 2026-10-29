# SPDX-FileCopyrightText: 2026 CommanderTurtle
# SPDX-License-Identifier: AGPL-3.0-only
"""Fetch version-specific documentation and declaration data, never execute it."""
from __future__ import annotations

import asyncio
import hashlib
import json
from pathlib import Path
import re
import tempfile

import httpx

from .generators.config_schema import extract as config_fields
from .generators.cli_schema import extract as cli_commands
from .state import private_write

CRATES = {"openshell-server", "openshell-core", "openshell-driver-docker", "openshell-driver-podman", "openshell-driver-kubernetes", "openshell-driver-vm"}


def build(source, target, pin):
    target.mkdir(parents=True, exist_ok=True, mode=0o700)
    pages, options = [], []
    for file in sorted((source / "docs").rglob("*.mdx")):
        raw = file.read_text(encoding="utf-8")
        relative = file.relative_to(source / "docs").as_posix()
        title = re.search(r'^title:\s*"?(.*?)"?\s*$', raw, re.M)
        body = re.sub(r"\A---\n.*?\n---\n", "", raw, flags=re.S)
        slug = relative.removesuffix(".mdx")
        private_write(target / "pages" / relative, raw)
        pages.append({"slug": slug, "title": title[1] if title else slug, "bytes": len(raw.encode()),
                      "sha256": hashlib.sha256(raw.encode()).hexdigest(), "text": body,
                      "source": f"https://github.com/NVIDIA/OpenShell/blob/{pin['commit']}/docs/{relative}"})
        heading = ""
        for line in body.splitlines():
            if line.startswith("#"):
                heading = line.lstrip("# ")
            if line.startswith("|") and "`" in line:
                cells = [x.strip() for x in re.split(r"(?<!\\)\|", line.strip("|"))]
                name = re.search(r"`([^`]+)`", cells[0]) if cells else None
                if name:
                    options.append({"name": name[1], "cells": cells, "section": heading, "page": slug})
    for file in (source / "docs/images").glob("*.svg"):
        private_write(target / "images" / file.name, file.read_bytes())
    if not pages:
        raise ValueError("Candidate has no documentation pages. Keep the current reference until this layout change is reviewed.")
    schema = config_fields(source, pin["commit"])
    commands = cli_commands(source)
    if not schema or not commands:
        raise ValueError("Upstream declarations changed layout. Review the generator before replacing the reference.")
    private_write(target / "catalog.json", json.dumps({"version": pin["version"], "commit": pin["commit"], "pages": pages, "options": options}, ensure_ascii=False))
    private_write(target / "config-fields.json", json.dumps(schema, ensure_ascii=False))
    private_write(target / "commands.json", json.dumps(commands, ensure_ascii=False))
    private_write(target / "LICENSE", (source / "LICENSE").read_bytes())
    return {"pages": len(pages), "settings": len(schema), "commands": len(commands)}


async def download(pin, root):
    target = root / "references" / pin["commit"]
    if (target / "ready.json").exists():
        return json.loads((target / "ready.json").read_text())
    if not re.fullmatch(r"[a-f0-9]{40}", pin["commit"]):
        raise ValueError("Invalid upstream commit.")
    async with httpx.AsyncClient(timeout=45, follow_redirects=True) as client:
        response = await client.get(f"https://api.github.com/repos/NVIDIA/OpenShell/git/trees/{pin['commit']}", params={"recursive": "1"})
        response.raise_for_status()
        tree = response.json()
        if tree.get("truncated"):
            raise ValueError("Upstream tree response was truncated. Reference synchronization needs review.")
        selected = []
        for entry in tree["tree"]:
            path = Path(entry["path"])
            parts = path.parts
            if entry["type"] != "blob" or ".." in parts or path.is_absolute():
                continue
            docs = len(parts) > 1 and parts[0] == "docs" and path.suffix in (".mdx", ".svg")
            rust = len(parts) > 3 and parts[0] == "crates" and parts[1] in CRATES and parts[2] == "src" and path.suffix == ".rs"
            if docs or rust or path.as_posix() in ("LICENSE", "crates/openshell-cli/src/main.rs"):
                selected.append(entry)
        if len(selected) > 2000 or sum(e.get("size", 0) for e in selected) > 64 * 1024 * 1024:
            raise ValueError("Reference exceeds synchronization budget. Review the new source layout.")
        semaphore = asyncio.Semaphore(4)
        with tempfile.TemporaryDirectory(prefix="reference-", dir=root) as temporary:
            source = Path(temporary)

            async def get(entry):
                async with semaphore:
                    url = f"https://raw.githubusercontent.com/NVIDIA/OpenShell/{pin['commit']}/{entry['path']}"
                    response = await client.get(url)
                    response.raise_for_status()
                    data = response.content
                    if len(data) > 4 * 1024 * 1024:
                        raise ValueError("Reference file exceeds 4 MiB.")
                    blob = hashlib.sha1(f"blob {len(data)}\0".encode() + data).hexdigest()
                    if blob != entry["sha"]:
                        raise ValueError(f"Upstream blob mismatch: {entry['path']}")
                    private_write(source / entry["path"], data)
            async with asyncio.TaskGroup() as tasks:
                for entry in selected:
                    tasks.create_task(get(entry))
            result = await asyncio.to_thread(build, source, target, pin)
    private_write(target / "ready.json", json.dumps(result))
    return result
