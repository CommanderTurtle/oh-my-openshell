# omosh

Oh My OpenShell. Local setup, configuration, named environments, tmux sessions, and an HTTP command bridge for [NVIDIA OpenShell](https://github.com/NVIDIA/OpenShell).

## Run

Python 3.11+, uv, and Bun are required to build from source. Docker Engine 28+ with Compose is required for the managed gateway. Use Linux, WSL2, or Apple Silicon macOS with Docker Desktop. WSL2 support follows upstream's experimental support status.

```sh
bun install --frozen-lockfile
bun run build
uv sync --frozen
uv run omosh --open
```

Open the private operator link printed in the terminal. The fragment key is exchanged for an HttpOnly, SameSite cookie and removed from the address bar. Keep the link private.

In Overview:

1. **Check** the host prerequisites.
2. **Install CLI** to download the checksum-verified pinned binary into omosh's state directory.
3. **Build image** to prepare bash, Python 3, and tmux inside a non-root workload image.
4. **Start gateway** to initialize mTLS and the owned Compose deployment.
5. Create an environment, then a named session. Use **Connect** for its live terminal.

Windows can run the dashboard and reference viewer. Run the application inside WSL2 for Docker and PTY access. The current upstream release supplies Linux and Apple Silicon CLI binaries.

## Configuration

The initial release pins **OpenShell v0.1.6**, including CLI archive checksums and gateway, supervisor, and sandbox image digests. The workload base is also digest-pinned. See [upstream.lock.json](upstream.lock.json).

Configuration includes source-derived controls for **197 gateway/driver settings**, **85 CLI command forms**, and **311 command arguments**. Nested arrays and maps have typed field editors with add, remove, reorder, enum choices, and JSON source. Provider-profile sources and isolation transports expose their variant-specific fields. The full TOML editor remains available. The bundled reference contains all **64 MDX pages**, diagrams, and **528 indexed table entries** from the pinned documentation tree.

**Save draft** preserves comments and unrelated TOML sections. Changes require a restart to apply. **Validate draft** checks local deployment conflicts; **Native preflight** uses the pinned gateway's own parser. Startup always runs native preflight. Version 2 TOML and authenticated local TLS are required by the managed deployment.

Conflict guidance links back to the relevant field, including middleware bindings, timeout and payload limits, profile-catalog registrations, and isolation-backend selection. Each indexed setting links to its reference chapter. Unsaved TOML stays in the current browser view while navigating between panels; save the draft to persist it across restarts.

Path controls can upload small operator files into the private, gateway-mounted state directory. Files keep owner-only permissions; native preflight checks their contents and any stricter upstream ownership requirements.

Managed lifecycle targets the Docker driver. The reference and settings index also expose upstream's Kubernetes, Podman, VM, OIDC, extension, provider, policy, and observability documentation. Those deployments require their upstream infrastructure; selecting another driver does not provision it automatically.

The CLI panel runs explicit argv against the isolated `omosh` registration. It covers providers, profiles, workspaces, members, policies, rule proposals, settings, service exposure, file transfer, and port forwarding. Native help and validation resolve constraints beyond the source-derived form hints. Interactive commands belong in the terminal; workbench calls have a bounded execution window.

## Sessions and bridge

Each environment is an OpenShell sandbox. Each named session is a tmux shell inside that sandbox. Commands enter through `openshell sandbox exec`; the Docker socket stays outside workload containers. Session creation checks the sandbox's immutable ID and owner label. Reusing a deleted sandbox's name requires a new registration.

Browser disconnects detach the terminal. The shell, working directory, exports, and background work remain in tmux. Closing a session explicitly ends its shell. **Fresh shell** recovers an exited pane or a restarted sandbox; it preserves a live pane and starts a new process only when needed. A gateway stop first stops registered owned sandboxes and preserves their data. Starting the gateway does not automatically restart stopped sandboxes.

The supplied workload includes an explicit filesystem policy for its working directory, home, temporary files, and PTY devices used by tmux. Outbound networking stays denied until a policy or provider grants it. A custom image or replacement policy must admit its own tmux/PTY paths and include bash, tmux, and Python 3. Global policies take precedence over the image policy.

Create a session-scoped token in **Agent bridge**, then set `OMOSH_TOKEN` in the calling process:

```sh
curl -sS http://127.0.0.1:8200/v1/sessions \
  -H "Authorization: Bearer $OMOSH_TOKEN"

curl -sS -X PUT http://127.0.0.1:8200/v1/sessions/SESSION_ID/commands \
  -H "Authorization: Bearer $OMOSH_TOKEN" \
  -H 'Content-Type: text/plain' \
  --data-binary @command.md

curl -sS -X DONE 'http://127.0.0.1:8200/v1/jobs/JOB_ID?wait=30' \
  -H "Authorization: Bearer $OMOSH_TOKEN"
```

`command.md` can contain plain shell text or one complete `bash`, `sh`, or `shell` Markdown fence. JSON input accepts `{"command":"pwd","timeout":300}`. Timeout is 1–3600 seconds. GET also retrieves DONE. DELETE on the job endpoint cancels it.

DONE returns Markdown with status, exit code, stdout, and stderr. A pending job remains available for a later request. One command runs at a time per session; separate sessions run concurrently. Terminal input pauses during a bridge command; Ctrl-C cancels it. A token can access only the sessions selected at issuance. Tokens can be revoked in the dashboard.

Bridge commands require the session to be at its shell prompt. Prompt hooks mark readiness without polling; a busy program, exited pane, or interrupted command after a dashboard restart blocks new command injection. A bridge command clears an unfinished prompt line before entering its wrapper. Replacing `PS0` or `PROMPT_COMMAND` removes that readiness contract; keep the hooks when customizing the shell.

Display output is bounded to 512 KiB per stream. Sandbox capture files retain up to 2 MiB per stream; excess output is drained. Dashboard previews use smaller excerpts. Commands are sourced in the persistent shell, so `exit`, `exec`, traps, shell options, and background processes retain their normal shell consequences. Use **Fresh shell** after a shell exits; its previous process state has ended.

The bridge uses localhost. For another machine, use an authenticated SSH tunnel to the local listener. `llm` integration is a separate future consumer of this protocol.

## State and lifecycle

Default: `${XDG_STATE_HOME:-$HOME/.local/state}/omosh`. Override with `--state-dir PATH`; use `--port PORT` for the dashboard listener.

```text
gateway.toml      Draft/active gateway configuration
compose.yaml      Owned Compose project
gateway/          Gateway database and PKI
client/           Isolated OpenShell CLI registration
bin/              Checksum-verified CLI versions
omosh.sqlite3     Session records, jobs, settings, token hashes
references/       Version-specific reference after an update
backups/          Verified offline SQLite snapshots before updates
```

The gateway runs as numeric root to access the Docker socket and its host-mounted state. Docker socket access is host-administrative access. Review the setup acknowledgement. Workloads run as a non-root image user. The gateway and initializer use same-path bind mounts so Docker sibling containers resolve extracted runtime binaries correctly. A deterministic project name isolates lifecycle commands from other Compose projects and OpenShell installations.

Gateway listeners bind `0.0.0.0` **inside the container**, so Docker can forward traffic to them. Compose publishes every listener on host `127.0.0.1` only. A container-loopback bind would make these published ports unreachable. The owned Compose network and gateway mTLS remain separate from host port publication.

The dashboard and CLI use separate operator credentials and session tokens. The dashboard binds `127.0.0.1`; host and origin checks protect local requests. Browser mutations require CSRF tokens. Raw bridge tokens are displayed once and stored as hashes. Upstream telemetry defaults off for the owned deployment and can be enabled explicitly.

The terminal is mounted only when attached, has bounded scrollback, acknowledges rendered output for backpressure, and coalesces resizes. Hiding the browser tab detaches its terminal view. Docker lifecycle events update gateway status while an operator view is connected. Update checks and prerequisite probes run on request rather than an idle polling timer.

Setup progress, terminal font size and scrollback, reference selection, selected session, and configuration view are saved per instance. Each operator can reconnect to the retained sessions from the dashboard.

Closing the browser or dashboard leaves the OpenShell gateway available. **Stop gateway** is the explicit shutdown action. Settings survive either operation. Removing omosh's Python package leaves its state directory and containers intact; stop owned environments/gateway before archiving that directory.

## Updates

**Check latest stable → Stage candidate → Apply staged update.** Staging verifies release hashes, registry digests, the required exec flags, and the real gateway configuration with the candidate image. The manual and generated forms are synchronized to the candidate's exact source commit; fetched files are checked against Git blob IDs. No downloaded source code is executed.

Versions shows the candidate release notes and compatibility report beside the installed artifact lock. Staging records the exact draft hash; editing that draft requires staging again before activation.

Applying stops registered sandboxes, stops the gateway, takes a verified SQLite backup, and changes all three runtime pins together. Review upstream release notes: v0.1.6 itself changed the supervisor/runtime connection protocol. Existing sandboxes retain their original runtime until recreated.

A failed candidate is stopped and the previous pin/config restored for inspection. Database downgrades require review of upstream migrations; the backup remains available. Infrastructure failures retain their actual errors. Breaking schema/CLI/source-layout changes stop staging rather than guessing a migration. Experimental driver and extension protocols require an operator compatibility review.

## Build a package

```sh
uv build --wheel
```

The wheel includes the built browser dependencies, the complete local reference, and a corresponding-source archive. Its runtime requires Python, the managed Docker installation, and the downloaded CLI; Bun is a source-build dependency.

To regenerate the reference from the exact pinned upstream checkout:

```sh
uv run python tools/import_reference.py /path/to/OpenShell
uv run python tools/config_schema.py /path/to/OpenShell
uv run python tools/cli_schema.py /path/to/OpenShell
```

See [architecture](docs/ARCHITECTURE.md) for the runtime, session, and update contracts.

## Licenses

omosh is licensed under **AGPL-3.0-only**. The dashboard footer provides its license and corresponding-source download, including the browser dependency sources. The archive includes project code and build files; instance data and credentials stay private. NVIDIA reference material retains its Apache-2.0 notices and [license](src/omosh/reference/LICENSE). Bundled dependencies retain their license notices. See [LICENSE](LICENSE) and [NOTICE](NOTICE).
