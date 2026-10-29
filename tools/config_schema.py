# SPDX-FileCopyrightText: 2026 CommanderTurtle
# SPDX-License-Identifier: AGPL-3.0-only
import json
from pathlib import Path
import sys
from omosh.generators.config_schema import extract

root = Path(__file__).resolve().parents[1]
pin = json.loads((root / "upstream.lock.json").read_text())
result = extract(Path(sys.argv[1]), pin["commit"])
(root / "src/omosh/reference/config-fields.json").write_text(json.dumps(result, ensure_ascii=False, indent=2), encoding="utf-8")
print(f"Indexed {len(result)} source-declared configuration fields.")
