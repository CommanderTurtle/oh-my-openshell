# SPDX-FileCopyrightText: 2026 CommanderTurtle
# SPDX-License-Identifier: AGPL-3.0-only
from pathlib import Path
import sys

from hatchling.builders.hooks.plugin.interface import BuildHookInterface


class CustomBuildHook(BuildHookInterface):
    def initialize(self, version, build_data):
        root = Path(self.root)
        sys.path.insert(0, str(root / "src"))
        from omosh.source import archive_source
        destination = root / "dist/omosh-source.tar.gz"
        destination.parent.mkdir(exist_ok=True)
        inherited = root / "src/omosh/source.tar.gz"
        destination.write_bytes(archive_source(root, inherited if inherited.is_file() else None))
        if self.target_name == "wheel":
            build_data.setdefault("force_include", {})[str(destination)] = "omosh/source.tar.gz"
        else:
            # Keep source distributions self-contained, including prebuilt assets.
            build_data.setdefault("force_include", {})[str(root / "src/omosh/static/vendor")] = "src/omosh/static/vendor"
            build_data["force_include"][str(destination)] = "src/omosh/source.tar.gz"
