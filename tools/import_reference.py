# SPDX-FileCopyrightText: 2026 CommanderTurtle
# SPDX-License-Identifier: AGPL-3.0-only
"""Vendor the pinned upstream manual and index its actual option tables/examples."""
import hashlib
import json
from pathlib import Path
import re
import shutil
import subprocess
import sys

root = Path(__file__).resolve().parents[1]
source = Path(sys.argv[1]).resolve()
pin = json.loads((root / "upstream.lock.json").read_text())
revision = subprocess.check_output(["git", "-C", str(source), "rev-parse", "HEAD"], text=True).strip()
if revision != pin["commit"]:
    raise SystemExit(f"Expected {pin['commit']}, found {revision}")
target = root / "src/omosh/reference"
target.mkdir(parents=True, exist_ok=True)
pages, options = [], []
for path in sorted((source / "docs").rglob("*.mdx")):
    raw = path.read_text(encoding="utf-8")
    relative = path.relative_to(source / "docs").as_posix()
    slug = relative.removesuffix(".mdx")
    title = re.search(r'^title:\s*"?(.*?)"?\s*$', raw, re.M)
    body = re.sub(r"\A---\n.*?\n---\n", "", raw, flags=re.S)
    # Keep source, including MDX and attribution. Rendering is separately sanitized.
    dest = target / "pages" / relative
    dest.parent.mkdir(parents=True, exist_ok=True)
    # Canonical UTF-8/LF bytes keep provenance hashes identical across checkouts.
    dest.write_bytes(raw.encode("utf-8"))
    pages.append({"slug": slug, "title": title[1] if title else slug, "bytes": len(raw.encode()),
                  "sha256": hashlib.sha256(raw.encode()).hexdigest(), "text": body,
                  "source": f"{pin['repository']}/blob/{pin['commit']}/docs/{relative}"})
    heading = ""
    for line in body.splitlines():
        if line.startswith("#"):
            heading = line.lstrip("# ")
        if line.startswith("|") and "`" in line:
            cells = [x.strip() for x in re.split(r"(?<!\\)\|", line.strip("|"))]
            name = re.search(r"`([^`]+)`", cells[0]) if cells else None
            if name:
                options.append({"name": name[1], "cells": cells, "section": heading, "page": slug})
catalog = {"version": pin["version"], "commit": revision, "pages": pages, "options": options}
(target / "catalog.json").write_text(json.dumps(catalog, ensure_ascii=False), encoding="utf-8")
for path in (source / "docs/images").glob("*.svg"):
    dest = target / "images" / path.name
    dest.parent.mkdir(exist_ok=True)
    shutil.copyfile(path, dest)
shutil.copyfile(source / "LICENSE", target / "LICENSE")
shutil.copyfile(root / "upstream.lock.json", target / "upstream.lock.json")
print(f"Indexed {len(pages)} pages and {len(options)} option rows at {revision}.")
