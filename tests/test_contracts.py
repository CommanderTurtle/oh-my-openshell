# SPDX-FileCopyrightText: 2026 CommanderTurtle
# SPDX-License-Identifier: AGPL-3.0-only
import asyncio
import json
from pathlib import Path
import sys
import shutil
import hashlib

from fastapi.testclient import TestClient
import pytest
import yaml

from omosh.config import compose, default_config, fields, load_pin, set_field, validate_config
from omosh.process import run
from omosh.runtime import Runtime, name
from omosh.server import create_app
from omosh.sessions import Jobs, Sessions, fence, job_markdown, parse_command
from omosh.state import State


@pytest.fixture
def state(tmp_path):
    return State(tmp_path / "state")


def test_config_defaults_are_authenticated_and_pinned(state):
    text = default_config(state.root, load_pin())
    report = validate_config(text)
    assert report["errors"] == []
    gateway = report["parsed"]["openshell"]["gateway"]
    assert gateway["mtls_auth"]["enabled"] is True
    assert gateway["auth"]["allow_unauthenticated_users"] is False
    assert gateway["bind_address"].startswith("0.0.0.0:")
    document = yaml.safe_load(compose(state.root, text, load_pin()))
    service = document["services"]["gateway"]
    assert "@sha256:" in service["image"]
    assert service["command"] == []
    assert service["environment"]["OPENSHELL_TELEMETRY_ENABLED"] == "false"
    assert all(p.startswith("127.0.0.1:") for p in service["ports"])
    assert service["volumes"][0]["source"] == service["volumes"][0]["target"]
    assert "privileged" not in service


def test_workload_policy_admits_tmux_without_blanket_devices():
    workload = Path(__file__).parents[1] / "src/omosh/workload"
    policy = yaml.safe_load((workload / "policy.yaml").read_text())
    fs = policy["filesystem_policy"]
    assert {"/dev/pts", "/dev/ptmx", "/dev/tty", "/home/sandbox"} <= set(fs["read_write"])
    assert "/dev" not in fs["read_write"]
    assert policy["network_policies"] == {}
    assert policy["landlock"]["compatibility"] == "hard_requirement"
    assert "COPY policy.yaml /etc/openshell/policy.yaml" in (workload / "Dockerfile").read_text()


def test_compose_preserves_literal_dollars_in_state_paths(state):
    root = state.root / "$LOCAL_STATE"
    document = compose(root, default_config(root, load_pin()), load_pin())
    assert "$$LOCAL_STATE" in document
    assert "$LOCAL_STATE" not in document.replace("$$LOCAL_STATE", "")


def test_bundled_reference_hashes_match_packaged_bytes():
    root = Path(__file__).parents[1] / "src/omosh/reference"
    catalog = json.loads((root / "catalog.json").read_text(encoding="utf-8"))
    for page in catalog["pages"]:
        data = (root / "pages" / (page["slug"] + ".mdx")).read_bytes()
        assert len(data) == page["bytes"]
        assert hashlib.sha256(data).hexdigest() == page["sha256"]


@pytest.mark.parametrize("field,value,needle", [
    ("openshell.version", 1, "version"),
    ("openshell.gateway.disable_tls", True, "authenticated"),
    ("openshell.gateway.auth.allow_unauthenticated_users", True, "authenticated"),
    ("openshell.gateway.bind_address", "127.0.0.1:9000", "inside the gateway container"),
    ("openshell.drivers.docker.sandbox_pids_limit", 0, "positive"),
    ("openshell.gateway.database_url", "sqlite:any.db", "environment-only"),
])
def test_conflicts(state, field, value, needle):
    text = set_field(default_config(state.root, load_pin()), field, value)
    assert needle in " ".join(validate_config(text)["errors"])


def test_comments_unknown_sections_and_arrays_survive(state):
    text = default_config(state.root, load_pin()) + '\n# retained comment\n[openshell.gateway.otlp]\nendpoint="http://localhost:4317"\n'
    changed = set_field(text, "openshell.gateway.log_level", "debug")
    assert "# retained comment" in changed
    assert 'endpoint="http://localhost:4317"' in changed
    assert "debug" in changed
    changed = set_field(changed, "openshell.gateway.server_sans", ["localhost", "host.openshell.internal"])
    assert any(f["path"].endswith("server_sans") and len(f["value"]) == 2 for f in fields(changed))


def test_tokens_are_scoped_hashed_and_revocable(state):
    token = state.mint_token("fixture", ["one"])
    assert state.token_scope(token) == ["one"]
    assert token not in json.dumps(state.tokens())
    assert state.token_scope("unknown") is None
    state.revoke(state.tokens()[0]["id"])
    assert state.token_scope(token) is None


def test_restart_preserves_settings_and_marks_orphan_jobs(state):
    state.put("sessions", "one", {"id": "one", "name": "shell"})
    state.put("jobs", "two", {"id": "two", "status": "running"})
    fresh = State(state.root)
    assert fresh.get("sessions", "one")["name"] == "shell"
    assert fresh.get("jobs", "two")["status"] == "interrupted"


@pytest.mark.parametrize("value", ["-x", "../../bad", "hello world", "UPPER", "a;touch-b", "", "x" * 33])
def test_names_reject_host_and_tmux_target_injection(value):
    with pytest.raises(ValueError):
        name(value)


def test_markdown_is_lossless():
    text = 'cd "/sandbox/my path"\nexport X="a b"\nprintf "%s\\n" "$X"'
    assert parse_command(f"```bash\n{text}\n```") == text + "\n"
    assert parse_command(text) == text + "\n"
    with pytest.raises(ValueError):
        parse_command("```bash\nunclosed")
    with pytest.raises(ValueError):
        parse_command("```bash\none\n```\n```bash\ntwo\n```")
    assert fence("hello\n```\nbye").startswith("````text")


async def test_process_bounds_output_and_preserves_exit_code():
    result = await run([sys.executable, "-c", "import sys;print('ok');sys.stderr.write('error');sys.exit(7)"], check=False)
    assert result["exit_code"] == 7
    assert result["stdout"].strip() == "ok"
    assert result["stderr"] == "error"
    result = await run([sys.executable, "-c", "print('x'*2200000)"], check=False)
    assert result["truncated"]
    assert len(result["stdout"]) == 2097152


async def test_process_timeout_is_bounded():
    with pytest.raises(asyncio.TimeoutError):
        await run([sys.executable, "-c", "import time;time.sleep(60)"], timeout=.1)


async def test_one_writer_per_session_parallel_sessions(state):
    async def publish(*_):
        pass
    release = asyncio.Event()
    jobs = Jobs(state, publish)

    async def blocked(job):
        await release.wait()
        return {"exit_code": 0, "stdout": "done"}
    a = jobs.create("a", blocked, "one")
    with pytest.raises(ValueError):
        jobs.create("collision", blocked, "one")
    b = jobs.create("b", blocked, "two")
    release.set()
    await asyncio.gather(*list(jobs.tasks.values()))
    assert state.get("jobs", a["id"])["status"] == "succeeded"
    assert state.get("jobs", b["id"])["status"] == "succeeded"
    assert not jobs.session_busy


def test_host_env_cannot_override_managed_gateway(state, monkeypatch):
    monkeypatch.setenv("OPENSHELL_GATEWAY_ENDPOINT", "https://example.invalid")
    monkeypatch.setenv("OPENSHELL_SUPERVISOR_IMAGE", "untrusted:latest")
    monkeypatch.setenv("DOCKER_HOST", "tcp://remote:2375")
    env = Runtime(state).env()
    assert "OPENSHELL_GATEWAY_ENDPOINT" not in env
    assert "OPENSHELL_SUPERVISOR_IMAGE" not in env
    assert "DOCKER_HOST" not in env
    assert Path(env["XDG_CONFIG_HOME"]).is_relative_to(state.root)


def test_bridge_cannot_access_operator_or_other_sessions(tmp_path):
    app = create_app(tmp_path, 8200)
    state = app.state.omosh
    state.put("sessions", "one", {"id": "one"})
    state.put("sessions", "two", {"id": "two"})
    state.put("jobs", "job", {"id": "job", "session": "two", "status": "succeeded"})
    token = state.mint_token("fixture", ["one"])
    with TestClient(app, base_url="http://127.0.0.1:8200") as client:
        assert client.get("/api/state").status_code == 401
        headers = {"Authorization": "Bearer " + token}
        assert client.get("/api/state", headers=headers).status_code == 403
        assert [s["id"] for s in client.get("/v1/sessions", headers=headers).json()] == ["one"]
        assert client.get("/v1/jobs/job", headers=headers).status_code == 403
        assert client.get("/api/state", headers={"Host": "attacker.invalid"}).status_code == 403


def test_browser_csrf_and_config_revision(tmp_path):
    app = create_app(tmp_path, 8200)
    with TestClient(app, base_url="http://127.0.0.1:8200") as client:
        assert client.post("/api/auth", json={"key": app.state.omosh.admin_key}).status_code == 200
        snapshot = client.get("/api/state").json()
        old = client.get("/api/config").json()
        assert client.put("/api/config", json={"text": old["text"], "revision": old["revision"]}).status_code == 403
        headers = {"X-Omosh-CSRF": snapshot["csrf"]}
        assert client.put("/api/config", json={"path": "openshell.gateway.log_level", "value": "debug", "revision": old["revision"]}, headers=headers).status_code == 200
        assert client.put("/api/config", json={"text": old["text"], "revision": old["revision"]}, headers=headers).status_code == 409
        assert client.get("/api/state", headers={"Origin": "https://attacker.invalid"}).status_code == 403


def test_reference_is_pinned_and_complete():
    from omosh.config import REFERENCE
    catalog = json.loads((REFERENCE / "catalog.json").read_text(encoding="utf-8"))
    assert catalog["commit"] == load_pin()["commit"]
    assert len(catalog["pages"]) == 64
    assert len(catalog["options"]) >= 500
    assert all(p["text"] and p["sha256"] and p["source"] for p in catalog["pages"])


def test_source_declared_forms_cover_native_contracts():
    from omosh.config import REFERENCE
    commands = json.loads((REFERENCE / "commands.json").read_text(encoding="utf-8"))
    assert len(commands) == 85
    assert sum(len(c["fields"]) for c in commands) == 311
    template = next(c for c in commands if c["command"] == ["sandbox", "create"])
    template_field = next(f for f in template["fields"] if f["key"] == "template")
    assert {"from", "gpu", "cpu", "memory", "driver_config_json", "envs", "env_from"} <= set(template_field["conflicts"])
    update = next(c for c in commands if c["command"] == ["policy", "update"])
    assert next(f for f in update["fields"] if f["key"] == "binaries")["flag"] == "--binary"
    assert next(f for f in update["fields"] if f["key"] == "binaries")["repeat"]
    schema = json.loads((REFERENCE / "config-fields.json").read_text(encoding="utf-8"))
    assert len(schema) == 197
    assert any(f["path"] == "openshell.gateway.otlp.ocsf.enabled" and f["type"] == "bool" for f in schema)
    assert not any(f["path"] == "openshell.gateway.database_url" for f in schema)


def test_process_owner_lock_is_released(tmp_path):
    from omosh.instance import instance_lock
    with instance_lock(tmp_path):
        with pytest.raises(RuntimeError):
            with instance_lock(tmp_path):
                pass
    with instance_lock(tmp_path):
        pass


async def test_health_alone_is_not_authentication(state, monkeypatch):
    runtime = Runtime(state)
    async def response(*args, **kwargs):
        return {"stdout": json.dumps({"status": "connected", "authentication": {"status": "failed"}}), "exit_code": 0}
    monkeypatch.setattr(runtime, "call", response)
    assert (await runtime.refresh())["state"] == "unauthenticated"


async def test_environment_ownership_is_immutable(state, monkeypatch):
    runtime = Runtime(state)
    environment = {"name": "same-name", "workspace": "default", "sandbox_id": "original"}
    async def response(*args, **kwargs):
        return {"stdout": json.dumps({"id": "replacement", "labels": {"omosh.owner": runtime.project}}), "exit_code": 0}
    monkeypatch.setattr(runtime, "call", response)
    with pytest.raises(RuntimeError, match="ownership changed"):
        await runtime.verify_environment(environment)


async def test_bridge_script_is_staged_not_typed_into_shell(state):
    import base64
    from types import SimpleNamespace
    calls = []
    environment = {"id": "default--fixture", "name": "fixture", "workspace": "default", "sandbox_id": "id"}
    state.put("environments", environment["id"], environment)
    state.put("sessions", "session", {"id": "session", "name": "shell", "environment": environment["id"], "tmux": "omosh-session"})
    async def publish(*_):
        pass
    async def verified(*_):
        return {"id": "id"}
    async def exec_(env, command, **kwargs):
        calls.append((command, kwargs))
        if "#{@omosh_ready} #{pane_dead}" in command:
            return {"stdout": "1 0\n", "exit_code": 0}
        if command[:2] == ["python3", "-c"] and len(command) > 3:
            # The bounded watcher returns binary-safe envelopes rather than expanding
            # control characters into potentially multi-megabyte JSON escapes.
            return {"stdout": json.dumps({"exit_code": 7, "stdout_b64": base64.b64encode(b"\0"*524288).decode(), "stderr_b64": "", "truncated": False})}
        return {"stdout": "", "exit_code": 0}
    runtime = SimpleNamespace(project="omosh-fixture", verify_environment=verified, exec=exec_)
    jobs = Jobs(state, publish)
    sessions = Sessions(state, runtime, jobs)
    script = 'export X="$(echo not-a-host-command)"\ncd "/sandbox/path with spaces"'
    job = await sessions.command("session", script, 20)
    await asyncio.gather(*list(jobs.tasks.values()))
    saved = state.get("jobs", job["id"])
    assert saved["exit_code"] == 7 and saved["status"] == "failed"
    assert saved["stdout"] == "\0"*524288
    preparation = next(options["stdin"] for argv, options in calls if options.get("stdin"))
    payload = json.loads(preparation)
    assert base64.b64decode(payload["script"]).decode() == script + "\n"
    wrapper = base64.b64decode(payload["wrapper"]).decode()
    assert "2097152" in wrapper
    if bash := shutil.which("bash"):
        await run([bash, "--noprofile", "--norc", "-n"], stdin=wrapper)
    assert script not in " ".join(" ".join(argv) for argv, _ in calls)
    send_keys = [argv for argv, _ in calls if "send-keys" in argv]
    assert len(send_keys) == 3
    assert send_keys[0][-1] == "C-u"
    assert send_keys[1][-1].startswith(". /tmp/omosh-fixture/")


def test_bad_bridge_json_and_unknown_hosts_are_rejected(tmp_path):
    app = create_app(tmp_path, 8200)
    state = app.state.omosh
    state.put("sessions", "one", {"id": "one"})
    token = state.mint_token("fixture", ["one"])
    with TestClient(app, base_url="http://127.0.0.1:8200") as client:
        headers = {"Authorization": "Bearer " + token}
        assert client.put("/v1/sessions/one/commands", json=[], headers=headers).status_code == 422
        assert client.put("/v1/sessions/one/commands", json={"command": "pwd", "timeout": True}, headers=headers).status_code == 422
        assert client.get("/healthz", headers={"Host": "testserver"}).status_code == 403
        assert client.get("/reference/../../admin.key").status_code == 404


def test_dashboard_job_summary_is_bounded():
    from omosh.server import job_summary
    summary = job_summary({"id": "one", "status": "succeeded", "stdout": "x" * 524288})
    assert len(summary["stdout"]) < 17000
    assert summary["truncated"]


async def test_queued_cancellation_releases_exclusive_lock(state):
    async def publish(*_):
        pass
    async def blocked(job):
        await asyncio.sleep(60)
    jobs = Jobs(state, publish)
    job = jobs.create("lifecycle", blocked, exclusive=True)
    with pytest.raises(ValueError):
        jobs.create("other", blocked)
    await jobs.cancel(job["id"])
    assert not jobs.exclusive and not jobs.tasks
    assert state.get("jobs", job["id"])["status"] == "cancelled"
    job = jobs.create("shell", blocked, session="one")
    await jobs.cancel(job["id"])
    assert not jobs.session_busy


async def test_managed_start_is_idempotent_and_uses_docker_cp(state, monkeypatch):
    import omosh.runtime as module
    runtime = Runtime(state)
    runtime.cli_path.parent.mkdir(parents=True)
    runtime.cli_path.write_bytes(b"fixture")
    calls = []
    monkeypatch.setattr(runtime, "require_host", lambda: None)
    monkeypatch.setattr(runtime, "docker", lambda *args: ["docker", *args])
    async def fake_run(argv, **kwargs):
        argv = list(map(str, argv))
        calls.append(argv)
        if "ps" in argv:
            return {"stdout": "a" * 64 + "\n", "exit_code": 0}
        if argv[:2] == ["docker", "cp"]:
            Path(argv[-1]).write_text("fixture certificate")
        if "gateway" in argv and "add" in argv:
            path = runtime.root / "client/openshell/gateways/omosh/metadata.json"
            path.write_text(json.dumps({"gateway_endpoint": "https://127.0.0.1:17670"}))
        return {"stdout": "", "stderr": "", "exit_code": 0}
    async def authenticated(*args, **kwargs):
        return {"stdout": json.dumps({"status": "connected", "authentication": {"status": "authenticated"}}), "exit_code": 0}
    monkeypatch.setattr(module, "run", fake_run)
    monkeypatch.setattr(runtime, "call", authenticated)
    assert (await runtime.start())["state"] == "reachable"
    assert (await runtime.start())["state"] == "reachable"
    assert sum("add" in c and "gateway" in c for c in calls) == 1
    assert sum(c[:2] == ["docker", "cp"] for c in calls) == 6
    assert not any("/bin/cat" in c for c in calls)
    preflight = next(c for c in calls if "preflight" in c)
    assert "--network" in preflight and "none" in preflight
    assert any("--force-recreate" in c for c in calls)


async def test_unrelated_compose_is_preserved(state):
    runtime = Runtime(state)
    path = state.root / "compose.yaml"
    path.write_text("services:\n  other:\n    image: example.invalid/other\n")
    before = path.read_text()
    with pytest.raises(RuntimeError, match="not marked as omosh-owned"):
        await runtime.prepare()
    assert path.read_text() == before


@pytest.mark.parametrize("readiness", ["0 0", "1 1", " 0", ""])
async def test_busy_or_untracked_shell_receives_no_bridge_keys(state, readiness):
    from types import SimpleNamespace
    state.put("environments", "env", {"id": "env", "name": "env", "workspace": "default"})
    state.put("sessions", "session", {"id": "session", "name": "shell", "environment": "env", "tmux": "omosh-session"})
    calls = []
    async def publish(*_):
        pass
    async def verify(*_):
        pass
    async def exec_(environment, argv, **kwargs):
        calls.append(argv)
        return {"stdout": readiness, "exit_code": 0}
    runtime = SimpleNamespace(project="omosh-fixture", verify_environment=verify, exec=exec_)
    jobs = Jobs(state, publish)
    sessions = Sessions(state, runtime, jobs)
    job = await sessions.command("session", "printf sentinel", 10)
    await asyncio.gather(*list(jobs.tasks.values()))
    assert state.get("jobs", job["id"])["status"] == "failed"
    assert not any("send-keys" in call for call in calls)
    assert not jobs.session_busy


async def test_session_prompt_hooks_are_local_to_exact_pane(state):
    from types import SimpleNamespace
    calls = []
    async def exec_(environment, argv, **kwargs):
        calls.append((argv, kwargs))
    runtime = SimpleNamespace(project="omosh-fixture", exec=exec_)
    sessions = Sessions(state, runtime, None)
    argv = await sessions.shell_argv({}, {"id": "one", "tmux": "omosh-one"})
    assert argv[:3] == ["bash", "--noprofile", "--rcfile"]
    payload = json.loads(calls[0][1]["stdin"])
    assert "omosh-one:0.0 @omosh_ready" in payload["content"]
    assert "PROMPT_COMMAND=__omosh_prompt" in payload["content"]
    assert "PS0=" in payload["content"]
    assert "sleep" not in payload["content"]
    if bash := shutil.which("bash"):
        await run([bash, "--noprofile", "--norc", "-n"], stdin=payload["content"])


async def test_update_failure_restores_pin_and_config(state, monkeypatch):
    import omosh.updates as module
    runtime = Runtime(state)
    original = (state.root / "gateway.toml").read_text()
    previous = runtime.pin
    candidate = {**previous, "version": "v0.2.0", "commit": "a" * 40,
                 "contracts": {"config_sha256": hashlib.sha256(original.encode()).hexdigest()}}
    state.put("config", "staged_pin", candidate)
    stopped = []
    async def stop():
        stopped.append(True)
    async def backup():
        return {"backup": "fixture.sqlite3"}
    async def start():
        assert runtime.pin == candidate
        raise RuntimeError("fixture authentication failure")
    async def prepare():
        assert runtime.pin == previous
    async def run(*args, **kwargs):
        return {"exit_code": 0}
    monkeypatch.setattr(runtime, "stop", stop)
    monkeypatch.setattr(runtime, "start", start)
    monkeypatch.setattr(runtime, "backup_database", backup)
    monkeypatch.setattr(runtime, "prepare", prepare)
    monkeypatch.setattr(runtime, "compose_args", lambda *a: ["fixture", *a])
    monkeypatch.setattr(module, "run", run)
    with pytest.raises(RuntimeError, match="fixture authentication failure"):
        await module.apply(runtime, type("Idle", (), {"session_busy": set()})())
    assert stopped
    assert runtime.pin == previous
    assert (state.root / "gateway.toml").read_text() == original
    assert state.get("config", "staged_pin") == candidate


async def test_staged_update_rejects_changed_draft_before_stopping(state, monkeypatch):
    from omosh.updates import apply
    runtime = Runtime(state)
    state.put("config", "staged_pin", {**runtime.pin, "contracts": {"config_sha256": "stale"}})
    async def forbidden():
        raise AssertionError("Lifecycle must remain untouched")
    monkeypatch.setattr(runtime, "stop", forbidden)
    with pytest.raises(ValueError, match="changed since staging"):
        await apply(runtime, type("Idle", (), {"session_busy": set()})())


def test_extension_conflicts_point_to_structured_fields(state):
    text = default_config(state.root, load_pin())
    text = set_field(text, "openshell.supervisor.middleware", [{"name": "guard", "grpc_endpoint": "https://guard:50051", "max_payload_bytes": 8192, "timeout": "2s", "binding_policy": "exact", "bindings": []}])
    report = validate_config(text)
    assert any(i["path"] == "openshell.supervisor.middleware" and "requires bindings" in i["message"] for i in report["issues"])
    text = set_field(text, "openshell.supervisor.middleware", [{"name": "guard", "grpc_endpoint": "https://guard:50051", "max_payload_bytes": 8192, "timeout": "2s", "binding_policy": "exact", "bindings": ["http_request_pre_credentials"]}])
    assert validate_config(text)["errors"] == []


def test_profile_sources_require_registered_interceptor(state):
    text = default_config(state.root, load_pin())
    text = set_field(text, "openshell.gateway.provider_profile_sources", [{"type": "interceptor", "name": "catalog"}])
    assert any("registered gateway interceptor" in e for e in validate_config(text)["errors"])
    text = set_field(text, "openshell.gateway.interceptors", [{"name": "catalog", "grpc_endpoint": "https://catalog:50051"}])
    assert validate_config(text)["errors"] == []


def test_corresponding_source_excludes_instance_state(tmp_path):
    import io
    import tarfile
    from omosh.source import archive_source
    for file, content in {"LICENSE": "GNU AFFERO GENERAL PUBLIC LICENSE", "README.md": "omosh", "src/omosh/server.py": "source", ".omosh/admin.key": "secret", ".env": "secret", "TODO.md": "private ledger"}.items():
        path = tmp_path / file
        path.parent.mkdir(parents=True, exist_ok=True)
        path.write_text(content)
    with tarfile.open(fileobj=io.BytesIO(archive_source(tmp_path)), mode="r:gz") as archive:
        assert set(archive.getnames()) == {"omosh/LICENSE", "omosh/README.md", "omosh/src/omosh/server.py"}


def test_preferences_and_full_config_maps_persist(tmp_path):
    app = create_app(tmp_path / "preferences")
    with TestClient(app, base_url="http://127.0.0.1:8200") as client:
        client.post("/api/auth", json={"key": app.state.omosh.admin_key})
        csrf = {"X-Omosh-CSRF": client.get("/api/state").json()["csrf"]}
        assert client.put("/api/preferences", json={"terminal_font_size": 15, "document": "about/architecture", "config_mode": "toml"}, headers=csrf).status_code == 200
        assert client.get("/api/state").json()["preferences"]["terminal_font_size"] == 15
        assert client.put("/api/preferences", json={"config_mode": "other"}, headers=csrf).status_code == 422
        assert client.get("/api/config").json()["values"]["openshell"]["gateway"]["mtls_auth"]["enabled"] is True
        assert "GNU AFFERO GENERAL PUBLIC LICENSE" in client.get("/license").text
