# SPDX-FileCopyrightText: 2026 CommanderTurtle
# SPDX-License-Identifier: AGPL-3.0-only
import json
from pathlib import Path
import sys
from omosh.generators.cli_schema import extract

root = Path(__file__).resolve().parents[1]
result = extract(Path(sys.argv[1]))
(root / "src/omosh/reference/commands.json").write_text(json.dumps(result, ensure_ascii=False, indent=2), encoding="utf-8")
print(f"Indexed {len(result)} CLI commands and {sum(len(c['fields']) for c in result)} argument controls.")
