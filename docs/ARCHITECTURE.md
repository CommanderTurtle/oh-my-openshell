# Architecture

## Boundaries

```text
browser ── cookie + CSRF ─┐
                         ├─ omosh / localhost
agent ── scoped bearer ──┘      │
                               ├─ owned Docker Compose lifecycle
                               └─ isolated OpenShell CLI / mTLS
                                          │
                                      supervisor
                                          │
                                    sandbox / tmux
```

Docker lifecycle calls use a dedicated project derived from the absolute state path. CLI calls use a private `XDG_CONFIG_HOME`; ambient `OPENSHELL_*`, `DOCKER_*`, and `COMPOSE_*` settings cannot redirect the installation. Host processes use argument arrays. Script text reaches only a verified OpenShell sandbox through stdin, with generated private job paths and literal tmux send-keys commands.

Gateway configuration is TOML schema 2. Saving is optimistic-concurrency controlled and atomic. Native preflight precedes activation. Structured edits preserve comments and unrelated tables. The generated setting index comes from upstream serde declarations; the command index comes from upstream clap declarations. These indexes are navigation and form hints, not replacement validators.

Nested editors retain the complete value, including unknown keys. Collapsed and paged children mount on demand. Tagged-union selectors replace only fields specific to the previous variant and seed required fields for the selected variant. TOML drafts survive navigation in the browser; individual field edits require saving or reloading that draft first.

The gateway's Docker socket is administratively powerful. Same-path host bind mounts follow the upstream container deployment. The operator must acknowledge this before startup. TLS client keys remain outside workload containers; supervisor authentication uses the upstream sandbox protocol and bearer tokens.

The container listens on `0.0.0.0`; Docker publishes its ports exclusively on host `127.0.0.1`. In v0.1.6, `gateway_listener.rs` binds exactly the configured address. The bundled upstream container guide's claim that the Docker driver adds a bridge listener is stale relative to that source; omosh follows the executable contract. Health and metrics publication follow the same container/host distinction.

The supplied image embeds `/etc/openshell/policy.yaml`. It keeps upstream-style filesystem restrictions and denied outbound networking, adding the sandbox home and `/dev/pts`, `/dev/ptmx`, `/dev/tty` for tmux. It requires filesystem enforcement. Custom and global policies remain authoritative; omosh never silently broadens them to make a terminal connect.

## Command execution

A session owns one tmux pane and a one-writer lock. The command is uploaded over managed exec into `/tmp/<instance>/<session>/<job>/script`. A generated wrapper sources it in that pane, records its return code atomically, and captures bounded output while mirroring it into the terminal. A single bounded exec connection waits for this job; idle sessions have no monitoring command. Cancellation sends Ctrl-C only to that pane.

The initial Bash rc file uses `PS0` and `PROMPT_COMMAND` to toggle a pane-local readiness option. Each bridge job checks it on demand before sending keys. A busy terminal or a still-running command after dashboard interruption is rejected rather than receiving shell text. Hooks are an interactive safety contract, not a security boundary against the sandbox user deliberately replacing its own shell integration.

Subsequent commands reuse shell cwd, exports, functions, and shell options. Shell control flow can intentionally terminate or replace the shell. Pane death is detected and reported with its exit status. Background processes have normal shell lifetime; the bridge timeout bounds waiting and collection, not arbitrary independent daemon lifetime. Session closure is the explicit process cleanup boundary.

The browser PTY attaches to `openshell sandbox exec --tty --no-login-shell -- tmux attach-session`. A fresh Python helper acquires its controlling TTY before exec, avoiding `preexec_fn` in the threaded server. Disconnect kills the attachment, not the tmux server. Renderer acknowledgements bound the unread WebSocket window to 8 KiB; input is capped per frame; dimensions and scrollback are bounded.

## Updates and reference provenance

The baseline lock was checked against the official stable release endpoint on 2026-10-29. Release v0.1.6 was published 2026-10-27. Each runtime image was independently resolved to its registry digest. The CLI archives use the GitHub release's SHA-256 digests. The workload base was independently resolved from Docker Hub.

Reference provenance is recorded in `reference/catalog.json`: every imported page has a pinned source URL, byte count, and content hash. The complete manual and diagrams are bundled, searchable, and synchronized to the selected runtime version.

Staging checks actual executable contracts rather than treating every minor/patch bump as compatible or breaking. It downloads source data for the exact commit, verifies Git blob identities, regenerates forms, and retains candidate release notes. Operator review remains necessary for experimental protocols and third-party extensions.

Compatibility reports distinguish a removed exec contract from a download, service, or native-preflight failure. A staged release carries the SHA-256 of the reviewed draft; activation compares it before stopping anything. Candidate preflight receives the same managed runtime image pins that activation writes. Gateway lifecycle operations reserve the job queue; ordinary commands continue independently across named sessions.

## Source distribution

omosh source uses AGPL-3.0-only. The public localhost `/license` and `/source.tar.gz` routes provide the license and corresponding source to dashboard users. Checkout archives are generated on request in a worker thread. Wheels embed the archive at build time, together with the root license and notices. An allowlist includes code, build inputs, reference material, and browser dependency sources; private instance directories, environment files, Git internals, and the working ledger are excluded.

## Dashboard map

| Workflow | Dashboard surface |
| --- | --- |
| Guided Docker, CLI, image, and mTLS setup | Overview |
| Named sandboxes and lifecycle | Environments |
| Persistent tmux shells and live terminals | Sessions |
| Typed advanced options, deployment settings, and conflict guidance | Configuration |
| Native upstream command forms and explicit argv | OpenShell CLI |
| Scoped localhost PUT/DONE protocol | Agent bridge |
| Complete pinned manual, diagrams, and search | Reference |
| Candidate releases, compatibility, and pinned artifacts | Versions |
| Command output, cancellation, and operation history | Activity |

## Deployment

The managed installer provisions the Docker-backed local gateway. Other OpenShell drivers and advanced extensions use their upstream infrastructure. The workbench supports bounded native CLI operations; interactive shell work belongs in Sessions. The localhost PUT/DONE bridge is the integration point for external clients, including a future `llm` consumer.
