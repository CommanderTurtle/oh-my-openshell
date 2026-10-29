# SPDX-FileCopyrightText: 2026 CommanderTurtle
# SPDX-License-Identifier: AGPL-3.0-only
"""Named tmux sessions reached exclusively through OpenShell's exec transport."""
from __future__ import annotations

import asyncio
import base64
import json
import re
import shlex
import time
import uuid

from .runtime import name

MAX_SCRIPT = 256 * 1024


def parse_command(text: str) -> str:
    if not text.strip() or len(text.encode()) > MAX_SCRIPT or "\0" in text:
        raise ValueError("Supply 1–262144 bytes of shell text, without NUL bytes.")
    match = re.fullmatch(r"\s*(`{3,}|~{3,})(?:bash|sh|shell|console)?[^\S\n]*\n(.*)\n\1\s*", text, re.S)
    if match:
        if re.search(r"^\s*" + re.escape(match[1]) + r"\s*$", match[2], re.M):
            raise ValueError("Send one shell code block per command.")
        return match[2] + "\n"
    if text.lstrip().startswith(("```", "~~~")):
        raise ValueError("Send one complete shell code fence or plain shell text.")
    return text + ("" if text.endswith("\n") else "\n")


def fence(text, language="text"):
    longest = max([len(x) for x in re.findall(r"`+", text)] or [0])
    marker = "`" * max(3, longest + 1)
    return f"{marker}{language}\n{text}\n{marker}"


def job_markdown(job):
    parts = [f"## {job['status']}", f"Job: `{job['id']}`", f"Session: `{job.get('session', 'operator')}`"]
    if "exit_code" in job:
        parts.append(f"Exit code: `{job['exit_code']}`")
    if job.get("error"):
        parts += ["### Error", fence(job["error"])]
    for stream in ("stdout", "stderr"):
        if stream in job:
            parts += [f"### {stream}", fence(job[stream])]
    if job.get("truncated"):
        parts.append("Display is limited to 512 KiB per stream. Capture files retain up to 2 MiB per stream; excess output is drained and discarded.")
    return "\n\n".join(parts) + "\n"


class Jobs:
    def __init__(self, state, publish):
        self.state, self.publish = state, publish
        self.tasks = {}
        self.session_busy = set()
        self.exclusive = False

    def create(self, title, action, session=None, exclusive=False):
        if self.exclusive or (exclusive and self.tasks):
            raise ValueError("An exclusive lifecycle change requires other jobs to finish first.")
        if len(self.tasks) >= 64:
            raise ValueError("64 jobs are active; wait for one to finish.")
        if session and session in self.session_busy:
            raise ValueError("This session already has an active command. Use another session or wait for DONE.")
        job = {"id": uuid.uuid4().hex, "title": title, "status": "queued", "created": time.time()}
        if session:
            job["session"] = session
            self.session_busy.add(session)
        if exclusive:
            self.exclusive = job["id"]
        try:
            self.state.put("jobs", job["id"], job)
        except BaseException:
            if session:
                self.session_busy.discard(session)
            if exclusive:
                self.exclusive = False
            raise

        async def work():
            try:
                job.update(status="running", started=time.time())
                self.state.put("jobs", job["id"], job)
                await self.publish("job", job)
                result = await action(job)
                if isinstance(result, dict) and "exit_code" in result:
                    job.update(result)
                    job["status"] = "succeeded" if result["exit_code"] == 0 else "failed"
                else:
                    job.update(status="succeeded", stdout=json.dumps(result, indent=2, ensure_ascii=False), exit_code=0)
            except asyncio.CancelledError:
                job.update(status="cancelled", error="Cancelled by the operator.")
            except Exception as error:
                job.update(status="failed", error=str(error))
            finally:
                job["ended"] = time.time()
                try:
                    self.state.put("jobs", job["id"], job)
                finally:
                    self._release(job)
                await self.publish("job", job)

        self.tasks[job["id"]] = asyncio.create_task(work())
        return job

    def _release(self, job):
        self.tasks.pop(job["id"], None)
        self.session_busy.discard(job.get("session"))
        if self.exclusive == job["id"]:
            self.exclusive = False

    async def cancel(self, job_id):
        task = self.tasks.get(job_id)
        if task:
            task.cancel()
            await asyncio.gather(task, return_exceptions=True)
            if self.tasks.get(job_id) is task:
                # A task cancelled before its first instruction never enters finally.
                job = self.state.get("jobs", job_id)
                job.update(status="cancelled", ended=time.time(), error="Cancelled before execution.")
                try:
                    self.state.put("jobs", job_id, job)
                finally:
                    self._release(job)
                await self.publish("job", job)

    async def close(self):
        await asyncio.gather(*(self.cancel(job_id) for job_id in list(self.tasks)), return_exceptions=True)


class Sessions:
    def __init__(self, state, runtime, jobs):
        self.state, self.runtime, self.jobs = state, runtime, jobs
        self.locks = {}

    def environment(self, environment_id):
        value = self.state.get("environments", environment_id)
        if not value:
            raise ValueError("Unknown environment.")
        return value

    def get(self, session_id):
        value = self.state.get("sessions", session_id)
        if not value:
            raise ValueError("Unknown session.")
        return value

    async def create_environment(self, spec):
        key = ("environment", name(spec["name"]), name(spec.get("workspace", "default")))
        async with self.locks.setdefault(key, asyncio.Lock()):
            return await self._create_environment(spec)

    async def _create_environment(self, spec):
        env_name, workspace = name(spec["name"]), name(spec.get("workspace", "default"))
        if len(workspace) > 19:
            raise ValueError("OpenShell workspace names are limited to 19 characters.")
        key = f"{workspace}--{env_name}"
        if self.state.get("environments", key):
            raise ValueError("This environment is already registered.")
        args = ["sandbox", "create", "--name", env_name, "--detach", "--no-auto-providers", "--label", f"omosh.owner={self.runtime.project}"]
        template = spec.get("template", "").strip()
        image = spec.get("image", "omosh-workload:local").strip()
        if template:
            if any(spec.get(k) for k in ("cpu", "memory", "gpu", "env")) or image != "omosh-workload:local":
                raise ValueError("A template owns its image, resources, and environment. Clear those overrides first.")
            args += ["--template", name(template)]
        else:
            if not image or len(image) > 512 or image.startswith("-") or any(c.isspace() for c in image):
                raise ValueError("Supply a container image reference.")
            args += ["--from", image]
            for option in ("cpu", "memory"):
                if spec.get(option):
                    args += [f"--{option}", str(spec[option])]
            if spec.get("gpu"):
                count = int(spec["gpu"])
                if count < 1 or count > 128:
                    raise ValueError("GPU count must be between 1 and 128.")
                args += ["--gpu", str(count)]
            for value in spec.get("env", []):
                if not re.fullmatch(r"[A-Za-z_][A-Za-z0-9_]*=[^\n\r\0]{0,8192}", value):
                    raise ValueError("Environment entries use KEY=VALUE, one non-secret entry per line.")
                args += ["--env", value]
        for provider in spec.get("providers", []):
            args += ["--provider", name(provider)]
        for value in spec.get("forward", []):
            if not re.fullmatch(r"(?:127\.0\.0\.1:)?[1-9][0-9]{0,4}", value):
                raise ValueError("Forward entries use [127.0.0.1:]PORT.")
            args += ["--forward", value]
        policy = spec.get("policy", "").strip()
        if policy:
            path = self.state.root / "policies" / f"{key}.yaml"
            from .state import private_write
            import yaml
            parsed = yaml.safe_load(policy)
            if not isinstance(parsed, dict) or parsed.get("version") != 1:
                raise ValueError("Policy must be a version: 1 YAML object. OpenShell performs authoritative validation.")
            private_write(path, policy)
            args += ["--policy", str(path)]
        # Main process remains alive; tools including tmux must already be in the image.
        args += ["--", "sleep", "infinity"]
        result = await self.runtime.call(args, workspace, timeout=180)
        inspect = await self.runtime.call(["sandbox", "get", env_name, "--output", "json"], workspace, timeout=20)
        sandbox = json.loads(inspect["stdout"])
        if not sandbox.get("id") or sandbox.get("labels", {}).get("omosh.owner") != self.runtime.project:
            raise RuntimeError("Created sandbox could not be verified as owned by this instance.")
        environment = {"id": key, "name": env_name, "workspace": workspace, "image": image, "created": time.time()}
        environment["sandbox_id"] = sandbox["id"]
        self.state.put("environments", key, environment)
        return {"environment": environment, "output": result["stdout"]}

    async def create(self, environment_id, title):
        name(title)
        async with self.locks.setdefault(("session", environment_id, title), asyncio.Lock()):
            return await self._create(environment_id, title)

    async def _create(self, environment_id, title):
        environment = self.environment(environment_id)
        await self.runtime.verify_environment(environment)
        name(title)
        if any(s["environment"] == environment_id and s["name"] == title for s in self.state.list("sessions")):
            raise ValueError("This environment already has a session with that name.")
        session = {"id": uuid.uuid4().hex, "name": title, "environment": environment_id, "created": time.time()}
        session["tmux"] = "omosh-" + session["id"]
        cwd = (await self.runtime.exec(environment, ["pwd"]))["stdout"].strip()
        if not cwd.startswith("/") or "\n" in cwd or len(cwd) > 4096:
            raise RuntimeError("Sandbox reported an invalid working directory.")
        session["workdir"] = cwd
        shell = await self.shell_argv(environment, session)
        await self.runtime.exec(environment, ["tmux", "-L", self.runtime.project, "new-session", "-d", "-s", session["tmux"],
                                              "-c", cwd, *shell])
        try:
            await self.runtime.exec(environment, ["tmux", "-L", self.runtime.project, "set-option", "-t", session["tmux"], "remain-on-exit", "on"])
        except Exception:
            await self.runtime.exec(environment, ["tmux", "-L", self.runtime.project, "kill-session", "-t", session["tmux"]], check=False)
            raise
        self.state.put("sessions", session["id"], session)
        return session

    async def shell_argv(self, environment, session):
        """Prompt hooks distinguish a ready shell from a program using its TTY."""
        path = f"/tmp/{self.runtime.project}/sessions/{session['id']}/bashrc"
        target = session["tmux"] + ":0.0"
        hook = shlex.join(["command", "tmux", "-L", self.runtime.project, "set-option", "-p", "-t", target, "@omosh_ready"])
        content = (f"__omosh_prompt() {{ {hook} 1 >/dev/null 2>&1; }}\n"
                   "PROMPT_COMMAND=__omosh_prompt\n"
                   f"PS0={shlex.quote('$(' + hook + ' 0 >/dev/null 2>&1)')}\n"
                   "PS1='\\u@\\h:\\w\\$ '\n")
        prepare = """import json,os,pathlib,sys
p=json.load(sys.stdin); path=pathlib.Path(p['path']); path.parent.mkdir(parents=True,exist_ok=True,mode=0o700)
fd=os.open(path,os.O_WRONLY|os.O_CREAT|os.O_TRUNC,0o600)
with os.fdopen(fd,'w') as out:out.write(p['content'])
"""
        await self.runtime.exec(environment, ["python3", "-c", prepare], stdin=json.dumps({"path": path, "content": content}))
        return ["bash", "--noprofile", "--rcfile", path, "-i"]

    def terminal_argv(self, session):
        environment = self.environment(session["environment"])
        return self.runtime.cli(["sandbox", "exec", environment["name"], "--no-login-shell", "--tty", "--",
                                 "tmux", "-L", self.runtime.project, "attach-session", "-t", session["tmux"]], environment["workspace"])

    async def delete(self, session_id, reserved=False):
        if session_id in self.jobs.session_busy and not reserved:
            raise ValueError("Cancel the active command before closing this session.")
        session = self.get(session_id)
        await self.runtime.verify_environment(self.environment(session["environment"]))
        environment = self.environment(session["environment"])
        probe = await self.runtime.exec(environment, ["tmux", "-L", self.runtime.project, "has-session", "-t", session["tmux"]], check=False)
        if probe["exit_code"] == 0:
            await self.runtime.exec(environment, ["tmux", "-L", self.runtime.project, "kill-session", "-t", session["tmux"]])
        self.state.delete("sessions", session_id)

    async def revive(self, session_id, reserved=False):
        if session_id in self.jobs.session_busy and not reserved:
            raise ValueError("Cancel the active command before starting a fresh shell.")
        session = self.get(session_id)
        environment = self.environment(session["environment"])
        await self.runtime.verify_environment(environment)
        probe = await self.runtime.exec(environment, ["tmux", "-L", self.runtime.project, "has-session", "-t", session["tmux"]], check=False)
        target = session["tmux"] + ":0.0"
        if probe["exit_code"] == 0:
            pane = await self.runtime.exec(environment, ["tmux", "-L", self.runtime.project, "display-message", "-p", "-t", target, "#{pane_dead}"])
            if pane["stdout"].strip() != "1":
                raise ValueError("The shell is still alive. Connect to it instead.")
            shell = await self.shell_argv(environment, session)
            command = ["respawn-pane", "-t", target, "-c", session.get("workdir", "/sandbox"), *shell]
        else:
            shell = await self.shell_argv(environment, session)
            command = ["new-session", "-d", "-s", session["tmux"], "-c", session.get("workdir", "/sandbox"), *shell]
        await self.runtime.exec(environment, ["tmux", "-L", self.runtime.project, *command])
        await self.runtime.exec(environment, ["tmux", "-L", self.runtime.project, "set-option", "-t", session["tmux"], "remain-on-exit", "on"])
        return {"session": session_id, "status": "Fresh shell started; previous shell process state was not restored."}

    async def command(self, session_id, script, timeout):
        script = parse_command(script)
        if not 1 <= timeout <= 3600:
            raise ValueError("Timeout must be 1–3600 seconds.")
        session = self.get(session_id)
        environment = self.environment(session["environment"])

        async def execute(job):
            await self.runtime.verify_environment(environment)
            target = session["tmux"] + ":0.0"
            ready = await self.runtime.exec(environment, ["tmux", "-L", self.runtime.project, "display-message", "-p", "-t", target, "#{@omosh_ready} #{pane_dead}"])
            if ready["stdout"].strip() != "1 0":
                raise ValueError("The shell is busy, exited, or has no readiness hook. Connect to the session and return to its prompt before retrying.")
            path = f"/tmp/{self.runtime.project}/{session['id']}/{job['id']}"
            encoded = base64.b64encode(script.encode()).decode()
            # Script bytes never enter send-keys; only a generated, quoted wrapper path does.
            tee = """import os,pathlib,sys
p=pathlib.Path(sys.argv[1]); used=0
with p.open('wb',buffering=0) as out:
 while True:
  block=os.read(0,8192)
  if not block:break
  data=block[:max(0,2097152-used)];used+=len(block)
  out.write(data)
  if data:
   try:os.write(1,data)
   except BrokenPipeError:pass
 if used>2097152:p.with_suffix('.truncated').touch()
p.with_suffix('.done').touch()
"""
            wrapper = (f"exec {{__omosh_out}}> >(python3 -c {shlex.quote(tee)} {shlex.quote(path + '/stdout')})\n"
                       f"exec {{__omosh_err}}> >(python3 -c {shlex.quote(tee)} {shlex.quote(path + '/stderr')} >&2)\n"
                       f". {shlex.quote(path + '/script')} >&$__omosh_out 2>&$__omosh_err\n"
                       "__omosh_rc=$?\n"
                       "exec {__omosh_out}>&- {__omosh_err}>&-\n"
                       f"for __omosh_drain in {{1..100}}; do [[ -e {shlex.quote(path+'/stdout.done')} && -e {shlex.quote(path+'/stderr.done')} ]] && break; sleep .02; done\n"
                       f"printf '%s\\n' \"$__omosh_rc\" >{shlex.quote(path + '/exit.tmp')}\n"
                       f"mv {shlex.quote(path + '/exit.tmp')} {shlex.quote(path + '/exit')}\n")
            payload = json.dumps({"path": path, "script": encoded, "wrapper": base64.b64encode(wrapper.encode()).decode()})
            prepare = """import base64,json,os,sys
p=json.load(sys.stdin); os.makedirs(p['path'],mode=0o700,exist_ok=False)
for n in ('script','wrapper'):
 fd=os.open(p['path']+'/'+n,os.O_WRONLY|os.O_CREAT|os.O_EXCL,0o600)
 with os.fdopen(fd,'wb') as f:f.write(base64.b64decode(p[n]))
"""
            await self.runtime.exec(environment, ["python3", "-c", prepare], stdin=payload)
            try:
                # Clear an unfinished prompt line only after the readiness check.
                await self.runtime.exec(environment, ["tmux", "-L", self.runtime.project, "send-keys", "-t", target, "C-u"])
                await self.runtime.exec(environment, ["tmux", "-L", self.runtime.project, "send-keys", "-t", target,
                                                      "-l", ". " + shlex.quote(path + "/wrapper")])
                await self.runtime.exec(environment, ["tmux", "-L", self.runtime.project, "send-keys", "-t", target, "Enter"])
                # One bounded connection watches this job's completion. Idle sessions do no work.
                watch = """import base64,json,pathlib,subprocess,sys,time
p=pathlib.Path(sys.argv[1]); deadline=time.monotonic()+int(sys.argv[2]); dead=None
while not (p/'exit').exists():
 if time.monotonic()>deadline: raise TimeoutError('Command timed out')
 probe=subprocess.run(['tmux','-L',sys.argv[3],'display-message','-p','-t',sys.argv[4],'#{pane_dead} #{pane_dead_status}'],capture_output=True,text=True)
 if probe.returncode: raise RuntimeError('tmux session disappeared')
 if probe.stdout.startswith('1 '):
  dead=int(probe.stdout.split()[1] or 1);break
 time.sleep(0.5)
rc=dead if dead is not None else int((p/'exit').read_text())
out={'exit_code':rc,'truncated':False}
for n in ('stdout','stderr'):
 try:
  with (p/n).open('rb') as f: data=f.read(524289)
 except FileNotFoundError:data=b''
 out[n+'_b64']=base64.b64encode(data[:524288]).decode();out['truncated']|=len(data)>524288 or (p/(n+'.truncated')).exists()
print(json.dumps(out))
"""
                result = await self.runtime.exec(environment, ["python3", "-c", watch, path, str(timeout), self.runtime.project, target], timeout=timeout + 2)
                output = json.loads(result["stdout"])
                for stream in ("stdout", "stderr"):
                    output[stream] = base64.b64decode(output.pop(stream + "_b64")).decode("utf-8", errors="replace")
                return output
            except BaseException:
                # Cancels only this pane, never the tmux server or another environment.
                try:
                    await asyncio.shield(self.runtime.exec(environment, ["tmux", "-L", self.runtime.project, "send-keys", "-t", target, "C-c"], timeout=5, check=False))
                except Exception:
                    pass
                raise

        return self.jobs.create("Shell command", execute, session_id)
