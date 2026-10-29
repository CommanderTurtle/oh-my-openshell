# SPDX-FileCopyrightText: 2026 CommanderTurtle
# SPDX-License-Identifier: AGPL-3.0-only
"""Corresponding-source archive for the checkout or installed distribution."""
from __future__ import annotations

import io
from pathlib import Path
import tarfile

ROOT_FILES = {"LICENSE", "NOTICE", "README.md", "pyproject.toml", "package.json", "uv.lock", "bun.lock", "upstream.lock.json", "hatch_build.py", ".gitignore", ".gitattributes"}
ROOT_DIRS = {"src/omosh", "tools", "web", "docs", "tests"}
DEPENDENCIES = ("@xterm/xterm", "@xterm/addon-fit", "marked", "dompurify")
EXCLUDED = {"__pycache__", ".pytest_cache", "source.tar.gz"}
MAX_SOURCE = 64 * 1024 * 1024


def checkout_root():
    root = Path(__file__).resolve().parents[2]
    return root if (root / "pyproject.toml").is_file() and (root / "tools/build.mjs").is_file() else None


def source_paths(root):
    paths = [root / name for name in ROOT_FILES if (root / name).is_file()]
    for prefix in (*ROOT_DIRS, *("node_modules/" + name for name in DEPENDENCIES)):
        directory = root / prefix
        if not directory.is_dir():
            continue
        for path in directory.rglob("*"):
            if path.is_file() and not path.is_symlink() and not EXCLUDED.intersection(path.relative_to(root).parts) and path.suffix != ".pyc":
                paths.append(path)
    return sorted(set(paths))


def archive_source(root, dependency_archive=None):
    root = Path(root).resolve()
    output, total = io.BytesIO(), 0
    with tarfile.open(fileobj=output, mode="w:gz") as archive:
        for path in source_paths(root):
            if not path.resolve().is_relative_to(root) or path.is_symlink():
                continue
            data = path.read_bytes()
            total += len(data)
            if total > MAX_SOURCE:
                raise ValueError("Source archive exceeds 64 MiB.")
            info = tarfile.TarInfo("omosh/" + path.relative_to(root).as_posix())
            info.size, info.mode, info.mtime = len(data), 0o644, 0
            archive.addfile(info, io.BytesIO(data))
        # A source distribution carries browser dependency sources even though
        # package builders normally exclude node_modules from the outer archive.
        if dependency_archive and not (root / "node_modules").is_dir():
            with tarfile.open(dependency_archive, mode="r:gz") as inherited:
                prefixes = tuple("omosh/node_modules/" + name + "/" for name in DEPENDENCIES)
                for member in inherited:
                    if not member.isfile() or not member.name.startswith(prefixes) or ".." in Path(member.name).parts:
                        continue
                    total += member.size
                    if total > MAX_SOURCE:
                        raise ValueError("Source archive exceeds 64 MiB.")
                    data = inherited.extractfile(member).read()
                    info = tarfile.TarInfo(member.name)
                    info.size, info.mode, info.mtime = len(data), 0o644, 0
                    archive.addfile(info, io.BytesIO(data))
    return output.getvalue()
