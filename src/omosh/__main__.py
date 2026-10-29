# SPDX-FileCopyrightText: 2026 CommanderTurtle
# SPDX-License-Identifier: AGPL-3.0-only
import argparse
import os
from pathlib import Path
import webbrowser

import uvicorn

from .server import create_app
from .instance import instance_lock


def main():
    parser = argparse.ArgumentParser(prog="omosh", description="Local OpenShell operator dashboard")
    parser.add_argument("--port", type=int, default=8200)
    parser.add_argument("--state-dir", type=Path, default=Path(os.environ.get("XDG_STATE_HOME", Path.home() / ".local/state")) / "omosh")
    parser.add_argument("--open", action="store_true", help="Open the operator link in a browser")
    args = parser.parse_args()
    if not 1024 <= args.port <= 65535:
        parser.error("port must be 1024–65535")
    os.umask(0o077)
    with instance_lock(args.state_dir.resolve()):
        app = create_app(args.state_dir.resolve(), args.port)
        if not (Path(__file__).parent / "static/vendor/vendor.js").exists():
            parser.error("Browser assets are missing. Run bun install --frozen-lockfile && bun run build first.")
        link = f"http://127.0.0.1:{args.port}/#key={app.state.omosh.admin_key}"
        print(f"omosh · http://127.0.0.1:{args.port}/\nOperator link (keep private): {link}", flush=True)
        if args.open:
            webbrowser.open(link)
        uvicorn.run(app, host="127.0.0.1", port=args.port, access_log=False, ws_max_size=32768, limit_concurrency=128)


if __name__ == "__main__":
    main()
