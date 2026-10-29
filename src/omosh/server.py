# SPDX-FileCopyrightText: 2026 CommanderTurtle
# SPDX-License-Identifier: AGPL-3.0-only
from __future__ import annotations

import asyncio
from contextlib import asynccontextmanager
import hashlib
import json
import os
from pathlib import Path
import secrets
import time
import tomllib
import uuid
from urllib.parse import urlsplit, unquote

from fastapi import FastAPI, HTTPException, Request, WebSocket
from fastapi.responses import FileResponse, JSONResponse, PlainTextResponse, StreamingResponse, Response
from fastapi.staticfiles import StaticFiles

from .config import REFERENCE, ensure_config, fields, set_field, remove_field, validate_config
from .runtime import Runtime
from .sessions import Jobs, Sessions, job_markdown
from .state import State, private_write
from . import updates
from .source import archive_source, checkout_root

STATIC = Path(__file__).parent / "static"


def create_app(root: Path, port=8200):
    state = State(root)
    runtime = Runtime(state)
    reference_cache = {}

    def reference_root():
        updated = root / "references" / runtime.pin["commit"]
        return updated if (updated / "ready.json").exists() else REFERENCE

    def reference_info():
        source = reference_root()
        if source not in reference_cache:
            reference_cache.clear()
            reference_cache[source] = json.loads((source / "catalog.json").read_text(encoding="utf-8"))
        return reference_cache[source]
    listeners = set()
    browser_sessions = {}
    observer = None

    async def publish(kind, value):
        if kind == "job":
            value = job_summary(value)
        event = f"event: {kind}\ndata: {json.dumps(value, ensure_ascii=False)}\n\n"
        for queue in tuple(listeners):
            if queue.full():
                try:
                    queue.get_nowait()
                except asyncio.QueueEmpty:
                    pass
            queue.put_nowait(event)

    jobs = Jobs(state, publish)
    sessions = Sessions(state, runtime, jobs)

    @asynccontextmanager
    async def lifespan(app):
        yield
        if observer:
            observer.cancel()
            await asyncio.gather(observer, return_exceptions=True)
        await jobs.close()

    app = FastAPI(title="omosh", version="0.1.0", docs_url=None, redoc_url=None, openapi_url=None, lifespan=lifespan)
    app.state.omosh = state
    app.state.runtime = runtime
    app.state.jobs = jobs
    app.state.sessions = sessions
    expected_hosts = {f"127.0.0.1:{port}", f"localhost:{port}", f"[::1]:{port}"}

    def authenticated(request):
        cookie = request.cookies.get("omosh_session", "")
        session = browser_sessions.get(cookie)
        if session and session["expires"] > time.time():
            return session
        return None

    def scoped(request, session_id=None):
        browser = authenticated(request)
        if browser:
            return
        token = request.headers.get("authorization", "").removeprefix("Bearer ")
        scope = state.token_scope(token) if token else None
        if scope is None or (session_id is not None and session_id not in scope):
            raise HTTPException(403, "Token does not grant access to this session.")

    def operator(request):
        if not authenticated(request):
            raise HTTPException(403, "Operator session required.")

    @app.middleware("http")
    async def security(request, call_next):
        if request.headers.get("host", "") not in expected_hosts:
            return PlainTextResponse("Unrecognized host", status_code=403)
        try:
            content_length = int(request.headers.get("content-length", "0") or 0)
        except ValueError:
            return PlainTextResponse("Invalid Content-Length", status_code=400)
        if content_length < 0 or content_length > 1024 * 1024:
            return PlainTextResponse("Request exceeds 1 MiB", status_code=413)
        origin = request.headers.get("origin")
        if origin and urlsplit(origin).netloc != request.headers.get("host"):
            return PlainTextResponse("Cross-origin access rejected", status_code=403)
        protected = request.url.path.startswith(("/api/", "/v1/")) and request.url.path != "/api/auth"
        if protected:
            browser = authenticated(request)
            token = request.headers.get("authorization", "").removeprefix("Bearer ")
            if not browser and (not token or state.token_scope(token) is None):
                return PlainTextResponse("Open the operator link printed by omosh, or supply a bridge bearer token.", status_code=401)
            if browser and request.method not in ("GET", "HEAD", "OPTIONS"):
                if not secrets.compare_digest(request.headers.get("x-omosh-csrf", ""), browser["csrf"]):
                    return PlainTextResponse("CSRF token required", status_code=403)
        response = await call_next(request)
        response.headers.update({
            "X-Content-Type-Options": "nosniff", "Referrer-Policy": "no-referrer", "Cache-Control": "no-store",
            "Content-Security-Policy": "default-src 'self'; script-src 'self'; style-src 'self' 'unsafe-inline'; img-src 'self' data:; connect-src 'self'; frame-ancestors 'none'; base-uri 'none'; form-action 'self'",
        })
        return response

    @app.exception_handler(ValueError)
    async def bad_value(request, error):
        return JSONResponse({"detail": str(error)}, status_code=422)

    @app.exception_handler(KeyError)
    async def missing_field(request, error):
        return JSONResponse({"detail": f"Missing required field: {error.args[0]}"}, status_code=422)

    @app.exception_handler(RuntimeError)
    async def runtime_error(request, error):
        return JSONResponse({"detail": str(error)}, status_code=409)

    @app.get("/healthz")
    async def health():
        return {"service": "omosh", "status": "ok"}

    @app.post("/api/auth")
    async def auth(request: Request):
        body = await body_json(request)
        if not secrets.compare_digest(str(body.get("key", "")), state.admin_key):
            raise HTTPException(401, "Invalid operator key.")
        now = time.time()
        for key in list(browser_sessions):
            if browser_sessions[key]["expires"] <= now:
                browser_sessions.pop(key)
        if len(browser_sessions) >= 32:
            raise HTTPException(429, "Close an existing operator session or restart omosh.")
        cookie = secrets.token_urlsafe(32)
        browser_sessions[cookie] = {"csrf": secrets.token_urlsafe(32), "expires": now + 24 * 3600}
        response = JSONResponse({"ok": True})
        response.set_cookie("omosh_session", cookie, httponly=True, samesite="strict", max_age=24 * 3600)
        return response

    @app.get("/api/state")
    async def snapshot(request: Request):
        operator(request)
        reference_metadata = reference_info()
        return {"csrf": authenticated(request)["csrf"], "pin": runtime.pin, "gateway": runtime.status,
                "doctor": state.get("config", "doctor", []), "environments": state.list("environments"),
                "sessions": state.list("sessions"), "jobs": [job_summary(j) for j in state.list("jobs", limit=100)], "tokens": state.tokens(),
                "telemetry": state.get("config", "telemetry", False), "state_dir": str(root),
                "manual": {"pages": len(reference_metadata["pages"]), "options": len(reference_metadata["options"]), "version": reference_metadata["version"]},
                "preferences": state.get("config", "preferences", {}), "setup": runtime.setup_state(),
                "updates": {"candidate": state.get("config", "candidate_pin"), "staged": state.get("config", "staged_pin"), "report": state.get("config", "compatibility")}}

    @app.put("/api/preferences")
    async def preferences(request: Request):
        operator(request)
        body = await body_json(request)
        allowed = {"session", "document", "config_mode", "terminal_font_size", "terminal_scrollback"}
        if set(body) - allowed:
            raise ValueError("Unknown preference.")
        current = state.get("config", "preferences", {})
        for key, value in body.items():
            if key in ("terminal_font_size", "terminal_scrollback"):
                lower, upper = (10, 24) if key == "terminal_font_size" else (500, 20000)
                if type(value) is not int or not lower <= value <= upper:
                    raise ValueError(f"{key} must be {lower}–{upper}.")
            elif not isinstance(value, str) or len(value) > 200:
                raise ValueError("Preference values must be short strings.")
            elif key == "config_mode" and value not in ("fields", "toml", "deployment"):
                raise ValueError("Choose fields, toml, or deployment.")
            current[key] = value
        state.put("config", "preferences", current)
        return current

    @app.get("/api/events")
    async def events(request: Request):
        nonlocal observer
        operator(request)
        queue = asyncio.Queue(maxsize=32)
        listeners.add(queue)
        if observer is None or observer.done() or observer.cancelling():
            observer = asyncio.create_task(runtime.observe(publish))

        async def stream():
            try:
                while authenticated(request) and not await request.is_disconnected():
                    try:
                        yield await asyncio.wait_for(queue.get(), 25)
                    except asyncio.TimeoutError:
                        yield ": keepalive\n\n"
            finally:
                listeners.discard(queue)
                if not listeners and observer:
                    observer.cancel()
        return StreamingResponse(stream(), media_type="text/event-stream", headers={"X-Accel-Buffering": "no"})

    @app.post("/api/actions/{action}")
    async def action_(action: str, request: Request):
        operator(request)
        actions = {"check": runtime.doctor, "install-cli": runtime.install_cli, "start": runtime.start,
                   "stop": runtime.stop, "refresh": runtime.refresh, "build-workload": runtime.build_workload,
                   "check-update": check_update, "stage-update": stage_update, "backup-database": runtime.backup_database,
                   "apply-update": lambda: updates.apply(runtime, jobs)}
        if action not in actions:
            raise HTTPException(404, "Unknown action")
        if action in ("start", "stop", "apply-update") and jobs.session_busy:
            raise ValueError("Wait for active bridge commands or cancel them before changing gateway state.")
        if any(state.get("jobs", job_id, {}).get("title") in ("start", "stop", "apply-update", "stage-update", "build-workload") for job_id in jobs.tasks):
            raise ValueError("An installation or lifecycle operation is already active.")
        return jobs.create(action, lambda job: actions[action](), exclusive=action in ("start", "stop", "install-cli", "stage-update", "apply-update", "build-workload", "backup-database"))

    async def check_update():
        pin = await updates.latest()
        staged = state.get("config", "staged_pin")
        if staged and staged["commit"] != pin["commit"]:
            state.delete("config", "staged_pin")
        state.put("config", "candidate_pin", pin)
        return {"installed": runtime.pin["version"], "candidate": pin["version"], "url": pin["release_url"], "notes": pin["notes"]}

    async def stage_update():
        pin = state.get("config", "candidate_pin")
        if not pin:
            raise ValueError("Check for a stable release first.")
        return await updates.stage(runtime, pin)

    @app.get("/api/config")
    async def config(request: Request):
        operator(request)
        text = ensure_config(state).read_text()
        return {"text": text, "fields": fields(text), "values": tomllib.loads(text), "revision": hashlib.sha256(text.encode()).hexdigest()}

    @app.post("/api/config/validate")
    async def config_validate(request: Request):
        operator(request)
        body = await body_json(request)
        report = validate_config(body["text"])
        report.pop("parsed")
        return report

    @app.put("/api/config")
    async def config_save(request: Request):
        operator(request)
        body = await body_json(request)
        if jobs.exclusive:
            raise ValueError("Wait for the installation or lifecycle operation before changing configuration.")
        async with runtime.lock:
            old = ensure_config(state).read_text()
            if body.get("revision") != hashlib.sha256(old.encode()).hexdigest():
                raise HTTPException(409, "Configuration changed in another view. Reload before saving.")
            text = (remove_field(old, body["path"]) if body.get("remove") else set_field(old, body["path"], body["value"])) if "path" in body else body["text"]
            report = validate_config(text)
            if report["errors"]:
                return JSONResponse({"detail": "\n".join(report["errors"]), "warnings": report["warnings"]}, status_code=422)
            # Local draft is persisted without requiring Docker; activation always runs native preflight.
            private_write(root / "gateway.toml.previous", old)
            private_write(root / "gateway.toml", text)
        return {"saved": True, "applied": False, "warnings": report["warnings"]}

    @app.post("/api/config/preflight")
    async def preflight(request: Request):
        operator(request)
        return jobs.create("Native preflight", lambda job: runtime.native_preflight(ensure_config(state).read_text()))

    @app.post("/api/files")
    async def upload(request: Request):
        operator(request)
        filename = unquote(request.headers.get("x-omosh-filename", ""))
        suffix = Path(filename).suffix.lower()
        if suffix not in (".pem", ".crt", ".key", ".yaml", ".yml", ".json", ".toml", ".txt"):
            raise ValueError("Upload a PEM, certificate, key, YAML, JSON, TOML, or text file.")
        content = await limited_body(request)
        if not content:
            raise ValueError("The selected file is empty.")
        destination = root / "gateway/operator" / (uuid.uuid4().hex + suffix)
        private_write(destination, content)
        return {"path": destination.as_posix(), "bytes": len(content), "detail": "Private operator file, also available at this path inside the owned gateway."}

    @app.put("/api/telemetry")
    async def telemetry(request: Request):
        operator(request)
        body = await body_json(request)
        if not isinstance(body.get("enabled"), bool):
            raise ValueError("enabled must be a boolean.")
        state.put("config", "telemetry", body["enabled"])
        return {"saved": True, "restart_required": True}

    @app.post("/api/environments")
    async def environment_create(request: Request):
        operator(request)
        body = await body_json(request)
        return jobs.create("Create environment", lambda job: sessions.create_environment(body), exclusive=True)

    @app.post("/api/environments/{environment_id}/{action}")
    async def environment_action(environment_id: str, action: str, request: Request):
        operator(request)
        if action not in ("start", "stop", "get"):
            raise ValueError("Choose start, stop, or get.")
        environment = sessions.environment(environment_id)
        args = ["sandbox", action, environment["name"]] + (["--output", "json"] if action == "get" else [])
        async def operate(job):
            await runtime.verify_environment(environment)
            return await runtime.call(args, environment["workspace"], timeout=60)
        return jobs.create(f"{action} {environment['name']}", operate, exclusive=action != "get")

    @app.post("/api/sessions")
    async def session_create(request: Request):
        operator(request)
        body = await body_json(request)
        return jobs.create("Create session", lambda job: sessions.create(body["environment"], body["name"]))

    @app.delete("/api/sessions/{session_id}")
    async def session_delete(session_id: str, request: Request):
        operator(request)
        sessions.get(session_id)
        return jobs.create("Close session", lambda job: sessions.delete(session_id, reserved=True), session=session_id)

    @app.post("/api/sessions/{session_id}/revive")
    async def revive(session_id: str, request: Request):
        operator(request)
        sessions.get(session_id)
        return jobs.create("Start fresh shell", lambda job: sessions.revive(session_id, reserved=True), session=session_id)

    @app.delete("/api/environments/{environment_id}")
    async def environment_delete(environment_id: str, request: Request):
        operator(request)
        body = await body_json(request)
        environment = sessions.environment(environment_id)
        if body.get("confirm_name") != environment["name"] or body.get("mode") not in ("delete", "unregister"):
            raise ValueError("Confirm the environment name and choose delete or unregister.")
        owned_sessions = [s for s in state.list("sessions") if s["environment"] == environment_id]
        if any(s["id"] in jobs.session_busy for s in owned_sessions):
            raise ValueError("Cancel active commands before removing this environment.")
        async def remove(job):
            if body["mode"] == "delete":
                await runtime.verify_environment(environment)
                await runtime.call(["sandbox", "delete", environment["name"]], environment["workspace"], timeout=120)
            for session in owned_sessions:
                state.delete("sessions", session["id"])
            state.delete("environments", environment_id)
            return {"environment": environment_id, "mode": body["mode"],
                    "detail": "Local registration removed." if body["mode"] == "unregister" else "Owned OpenShell sandbox deleted."}
        return jobs.create("Remove environment", remove, exclusive=True)

    @app.post("/api/tokens")
    async def token_create(request: Request):
        operator(request)
        body = await body_json(request)
        selected = body.get("sessions", [])
        if not isinstance(selected, list) or not selected or any(not isinstance(s, str) or not state.get("sessions", s) for s in selected):
            raise ValueError("Select at least one existing session.")
        return {"token": state.mint_token(str(body.get("label", "agent"))[:100], selected)}

    @app.delete("/api/tokens/{token_id}")
    async def token_delete(token_id: str, request: Request):
        operator(request)
        state.revoke(token_id)
        return {"revoked": True}

    @app.get("/v1/sessions")
    async def bridge_sessions(request: Request):
        token = request.headers.get("authorization", "").removeprefix("Bearer ")
        scope = state.token_scope(token) or []
        return [s for s in state.list("sessions") if authenticated(request) or s["id"] in scope]

    @app.put("/v1/sessions/{session_id}/commands", status_code=202)
    async def command(session_id: str, request: Request):
        scoped(request, session_id)
        raw = await limited_body(request, 262144)
        if request.headers.get("content-type", "").startswith("application/json"):
            body = json.loads(raw)
            if not isinstance(body, dict) or not isinstance(body.get("command"), str):
                raise ValueError("Supply a JSON object with a string command, or send plain shell text.")
            text, timeout = body["command"], body.get("timeout", 300)
            if isinstance(timeout, bool) or not isinstance(timeout, int):
                raise ValueError("timeout must be an integer number of seconds.")
        else:
            text, timeout = raw.decode("utf-8"), 300
        job = await sessions.command(session_id, text, timeout)
        return {"id": job["id"], "status": job["status"], "done": f"/v1/jobs/{job['id']}"}

    @app.api_route("/v1/jobs/{job_id}", methods=["GET", "DONE"])
    async def done(job_id: str, request: Request, wait: int = 0):
        job = state.get("jobs", job_id)
        if not job:
            raise HTTPException(404, "Unknown job")
        if job.get("session"):
            scoped(request, job["session"])
        else:
            operator(request)
        if not 0 <= wait <= 30:
            raise ValueError("wait must be 0–30 seconds.")
        task = jobs.tasks.get(job_id)
        if task and wait:
            await asyncio.wait([task], timeout=wait)
            job = state.get("jobs", job_id)
        return PlainTextResponse(job_markdown(job), media_type="text/markdown", headers={"X-Omosh-Status": job["status"]})

    @app.delete("/v1/jobs/{job_id}")
    async def cancel(job_id: str, request: Request):
        job = state.get("jobs", job_id)
        if not job:
            raise HTTPException(404, "Unknown job")
        if job.get("session"):
            scoped(request, job["session"])
        else:
            operator(request)
        await jobs.cancel(job_id)
        return {"cancelled": True}

    @app.post("/api/cli")
    async def cli(request: Request):
        operator(request)
        body = await body_json(request)
        argv = body.get("argv", [])
        allowed = {"status", "whoami", "doctor", "gateway", "workspace", "sandbox", "provider", "profile", "policy", "rule", "settings", "logs", "forward", "service"}
        if not isinstance(argv, list) or not argv or argv[0] not in allowed or len(argv) > 100 or any(not isinstance(s, str) or "\0" in s or len(s) > 8192 for s in argv):
            raise ValueError("Supply an OpenShell command argument array.")
        if not body.get("confirm") and "--help" not in argv and argv[0] not in ("status", "doctor"):
            raise ValueError("Confirm the displayed command before running it.")
        return jobs.create("openshell " + " ".join(argv[:3]), lambda job: runtime.call(argv, body.get("workspace", "default"), timeout=120))

    @app.websocket("/api/terminal/{session_id}")
    async def terminal(websocket: WebSocket, session_id: str):
        origin = websocket.headers.get("origin", "")
        host = websocket.headers.get("host", "")
        if host not in expected_hosts or urlsplit(origin).netloc != host or not authenticated(websocket):
            await websocket.close(code=1008)
            return
        from .terminal import attach
        try:
            session = sessions.get(session_id)
            await runtime.verify_environment(sessions.environment(session["environment"]))
            await attach(websocket, sessions, session)
        except (ValueError, RuntimeError):
            try:
                await websocket.close(code=1011)
            except RuntimeError:
                pass

    # Reference documents are public data, not operator state.
    @app.get("/source.tar.gz")
    async def source_download():
        checkout = checkout_root()
        if checkout:
            data = await asyncio.to_thread(archive_source, checkout)
            return Response(data, media_type="application/gzip", headers={"Content-Disposition": 'attachment; filename="omosh-source.tar.gz"'})
        return FileResponse(Path(__file__).parent / "source.tar.gz", media_type="application/gzip", filename="omosh-source.tar.gz")

    @app.get("/license")
    async def project_license():
        checkout = checkout_root()
        path = checkout / "LICENSE" if checkout else Path(__file__).parent / "legal/LICENSE"
        return PlainTextResponse(path.read_text(encoding="utf-8"))

    @app.get("/reference/{path:path}")
    async def reference_file(path: str):
        base = reference_root().resolve()
        candidate = (base / path).resolve()
        if not candidate.is_relative_to(base) or not candidate.is_file() or candidate.suffix not in (".json", ".svg", ".mdx"):
            raise HTTPException(404, "Unknown reference file")
        return FileResponse(candidate)
    app.mount("/assets", StaticFiles(directory=STATIC), name="assets")

    @app.get("/")
    async def index():
        return FileResponse(STATIC / "index.html")

    return app


async def limited_body(request, limit=1024 * 1024):
    result = bytearray()
    async for chunk in request.stream():
        result.extend(chunk)
        if len(result) > limit:
            raise HTTPException(413, "Request exceeds size limit")
    return bytes(result)


async def body_json(request):
    try:
        body = json.loads(await limited_body(request))
    except (json.JSONDecodeError, UnicodeDecodeError) as error:
        raise ValueError("Send valid UTF-8 JSON.") from error
    if not isinstance(body, dict):
        raise ValueError("Send a JSON object.")
    return body


def job_summary(job):
    result = dict(job)
    for stream in ("stdout", "stderr", "error"):
        if len(str(result.get(stream, ""))) > 16384:
            result[stream] = result[stream][:16384] + "\n[Dashboard preview bounded; retrieve the full DONE result.]"
            result["truncated"] = True
    return result
