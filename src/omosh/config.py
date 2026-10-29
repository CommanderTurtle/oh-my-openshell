# SPDX-FileCopyrightText: 2026 CommanderTurtle
# SPDX-License-Identifier: AGPL-3.0-only
from __future__ import annotations

import json
from pathlib import Path
import hashlib
import re
import tomllib

import tomlkit
import yaml

from .state import private_write

REFERENCE = Path(__file__).parent / "reference"


def load_pin():
    return json.loads((REFERENCE / "upstream.lock.json").read_text())


def default_config(root: Path, pin: dict) -> str:
    data = (root / "gateway").as_posix()
    doc = tomlkit.document()
    doc.add(tomlkit.comment("omosh-owned gateway. Validate before applying; settings persist across stops."))
    doc["openshell"] = {
        "version": 2,
        "gateway": {
            "name": "omosh", "bind_address": "0.0.0.0:17670",
            "health_bind_address": "0.0.0.0:17671", "log_level": "info", "compute_driver": "docker",
            "config_delivery_mode": "push", "policy_validation_failure_mode": "fail_closed",
            "auth": {"allow_unauthenticated_users": False},
            "mtls_auth": {"enabled": True},
            "guest_tls_ca": f"{data}/tls/ca.crt",
            "tls": {"cert_path": f"{data}/tls/server/tls.crt", "key_path": f"{data}/tls/server/tls.key",
                    "client_ca_path": f"{data}/tls/ca.crt"},
            "gateway_jwt": {"signing_key_path": f"{data}/tls/jwt/signing.pem",
                            "public_key_path": f"{data}/tls/jwt/public.pem",
                            "kid_path": f"{data}/tls/jwt/kid", "gateway_id": "omosh"},
        },
        "drivers": {"docker": {
            "default_image": "omosh-workload:local", "sandbox_runtime_image": pin["images"]["sandbox"],
            "supervisor_image": pin["images"]["supervisor"], "image_pull_policy": "if_not_present",
            "sandbox_label": "omosh-" + hashlib.sha256(str(root).encode()).hexdigest()[:10],
            "supervisor_network_mode": "host", "enable_bind_mounts": False,
            "sandbox_pids_limit": 2048, "app_armor_profile": "Unconfined",
        }},
    }
    return tomlkit.dumps(doc)


def validate_config(text: str):
    if not isinstance(text, str):
        raise ValueError("Supply TOML text.")
    if len(text.encode()) > 1024 * 1024:
        raise ValueError("Configuration exceeds 1 MiB.")
    data = tomllib.loads(text)
    shell = data.get("openshell", {})
    if not isinstance(shell, dict) or not isinstance(shell.get("gateway", {}), dict):
        raise ValueError("openshell and openshell.gateway must be TOML tables.")
    gateway = shell.get("gateway", {})
    for parent, keys in ((shell, ("drivers", "supervisor")), (gateway, ("auth", "mtls_auth", "oidc"))):
        if any(key in parent and not isinstance(parent[key], dict) for key in keys):
            raise ValueError("Configuration sections must be TOML tables.")
    errors, warnings = [], []
    if shell.get("version") != 2:
        errors.append("This release requires [openshell] version = 2.")
    if "database_url" in gateway:
        errors.append("database_url is environment-only; use the managed database setting.")
    if gateway.get("compute_driver") != "docker":
        errors.append("Managed Compose lifecycle requires compute_driver = 'docker'. Other drivers remain available in the manual and on externally managed gateways.")
    if gateway.get("disable_tls") or gateway.get("auth", {}).get("allow_unauthenticated_users"):
        errors.append("The managed bridge requires authenticated TLS. Keep disable_tls and allow_unauthenticated_users false.")
    if gateway.get("mtls_auth", {}).get("enabled") is False:
        errors.append("The managed CLI uses mTLS user authentication; keep mtls_auth.enabled true.")
    if gateway.get("oidc"):
        errors.append("OIDC requires a separately registered gateway; the owned local gateway uses mTLS.")
    for key in ("bind_address", "health_bind_address", "metrics_bind_address"):
        if key not in gateway:
            continue
        if not isinstance(gateway[key], str) or not re.fullmatch(r"0\.0\.0\.0:[1-9][0-9]{0,4}", gateway[key]):
            errors.append(f"{key} must use 0.0.0.0:PORT inside the gateway container. Compose publishes the port only on host 127.0.0.1.")
        elif int(gateway[key].rsplit(":", 1)[1]) > 65535:
            errors.append(f"{key} port exceeds 65535.")
    listeners = [gateway[k] for k in ("bind_address", "health_bind_address", "metrics_bind_address") if k in gateway]
    if len(listeners) != len(set(map(str, listeners))):
        errors.append("Gateway, health, and metrics listeners need different ports.")
    for section in ("docker", "podman", "vm"):
        driver = shell.get("drivers", {}).get(section, {})
        if not isinstance(driver, dict):
            errors.append(f"openshell.drivers.{section} must be a TOML table.")
            continue
        if driver.get("enable_bind_mounts"):
            warnings.append("Host bind mounts expose host files to workloads. Review the isolation boundary before applying.")
        if driver.get("sandbox_pids_limit") == 0:
            errors.append("sandbox_pids_limit must be positive.")
        if driver.get("proxy_ca_bundle") and not driver.get("https_proxy"):
            errors.append("proxy_ca_bundle requires https_proxy.")
    a, b = gateway.get("grpc_rate_limit_requests", 0), gateway.get("grpc_rate_limit_window_seconds", 0)
    if bool(a) != bool(b):
        warnings.append("Rate limiting is disabled unless both request count and window are positive.")
    if gateway.get("policy_validation_failure_mode") == "retain_last_valid":
        warnings.append("Invalid replacement policies keep the previous valid policy active.")
    issues = extension_conflicts(shell)
    errors.extend(i["message"] for i in issues if i["severity"] == "error")
    warnings.extend(i["message"] for i in issues if i["severity"] == "warning")
    return {"errors": errors, "warnings": warnings, "issues": issues, "parsed": data, "native_preflight_required": True}


def extension_conflicts(shell):
    """Local cross-field guidance; the selected gateway remains authoritative."""
    issues = []

    def issue(path, message, severity="error"):
        issues.append({"path": path, "message": message, "severity": severity})

    def registrations(parent, key, path):
        entries = parent.get(key, [])
        if not isinstance(entries, list) or any(not isinstance(entry, dict) for entry in entries):
            issue(path, f"{path} must be an array of tables.")
            return []
        names = set()
        for entry in entries:
            name = entry.get("name")
            if not isinstance(name, str) or not name.strip() or name in names:
                issue(path, f"{path} needs unique, nonempty registration names.")
            else:
                names.add(name)
        return entries

    gateway, supervisor = shell.get("gateway", {}), shell.get("supervisor", {})
    interceptors = registrations(gateway, "interceptors", "openshell.gateway.interceptors")
    middlewares = registrations(supervisor, "middleware", "openshell.supervisor.middleware")
    backends = registrations(supervisor, "isolation_backends", "openshell.supervisor.isolation_backends")
    for entries, path in ((interceptors, "openshell.gateway.interceptors"), (middlewares, "openshell.supervisor.middleware")):
        for entry in entries:
            name = entry.get("name", "service")
            endpoint = entry.get("grpc_endpoint", "")
            if not isinstance(endpoint, str) or not endpoint.startswith(("https://", "http://", "unix://")) or endpoint in ("http://", "https://", "unix://"):
                issue(path, f"{name}: supply a gRPC service endpoint.")
            elif endpoint.startswith("http://") and not entry.get("allow_insecure_transport"):
                issue(path, f"{name}: plaintext transport requires an explicit allow_insecure_transport override.")
            elif endpoint.startswith("http://"):
                issue(path, f"{name}: plaintext extension traffic has no bearer authentication.", "warning")
            mode, bindings = entry.get("binding_policy", "dynamic"), entry.get("bindings", [])
            if mode in ("allowlist", "exact") and not bindings:
                issue(path, f"{name}: {mode} binding policy requires bindings.")
            if path.endswith("middleware"):
                if mode == "dynamic" and bindings:
                    issue(path, f"{name}: explicit middleware bindings require allowlist or exact binding policy.")
                if isinstance(name, str) and name.startswith("openshell/"):
                    issue(path, f"{name}: openshell/ is reserved for built-in middleware.")
                size = entry.get("max_payload_bytes", entry.get("max_body_bytes"))
                if type(size) is not int or not 0 <= size <= 4 * 1024 * 1024:
                    issue(path, f"{name}: max_payload_bytes must be 0–4194304; payload-bearing bindings require a positive limit.")
                duration = re.fullmatch(r"([0-9]+)(ms|s)", str(entry.get("timeout", "500ms")))
                milliseconds = int(duration[1]) * (1000 if duration[2] == "s" else 1) if duration else 0
                if not 10 <= milliseconds <= 30000:
                    issue(path, f"{name}: timeout must be an integer duration from 10ms through 30s.")
    names = {e["name"] for e in interceptors if isinstance(e.get("name"), str)}
    sources = gateway.get("provider_profile_sources", [])
    source_path = "openshell.gateway.provider_profile_sources"
    if not isinstance(sources, list) or any(not isinstance(entry, dict) for entry in sources):
        issue(source_path, "provider_profile_sources must be an array of source tables.")
    else:
        for entry in sources:
            if entry.get("type") == "builtin":
                issue(source_path, "Profiles are import-only. Choose user or a registered interceptor source; import profiles with the OpenShell CLI.")
            elif entry.get("type") == "interceptor" and (not isinstance(entry.get("name"), str) or entry["name"] not in names):
                issue(source_path, "An interceptor profile source must name a registered gateway interceptor.")
    backend_names = {e["name"] for e in backends if isinstance(e.get("name"), str)}
    for driver_name, driver in shell.get("drivers", {}).items():
        if isinstance(driver, dict) and "isolation_backend" in driver and (not isinstance(driver["isolation_backend"], str) or driver["isolation_backend"] != "openshell-sandbox" and driver["isolation_backend"] not in backend_names):
            issue(f"openshell.drivers.{driver_name}.isolation_backend", "Selected isolation backend must have an operator-owned supervisor registration.")
    return issues


def fields(text):
    result = []

    def visit(value, prefix=""):
        for key, item in value.items():
            path = f"{prefix}.{key}" if prefix else key
            if isinstance(item, dict):
                visit(item, path)
            else:
                result.append({"path": path, "value": item, "type": type(item).__name__})
    visit(tomllib.loads(text))
    return result


def set_field(text, path, value):
    # Field updates preserve TOML comments and unrelated sections.
    if not re.fullmatch(r"openshell(?:\.[A-Za-z0-9_-]+)+", path):
        raise ValueError("Select a dotted OpenShell field path.")
    doc = tomlkit.parse(text)
    section = doc
    *parents, key = path.split(".")
    for parent in parents:
        if parent not in section:
            section[parent] = tomlkit.table()
        section = section[parent]
        if not isinstance(section, dict):
            raise ValueError("This field crosses an array or scalar; edit its TOML section instead.")
    section[key] = value
    return tomlkit.dumps(doc)


def remove_field(text, path):
    if not re.fullmatch(r"openshell(?:\.[A-Za-z0-9_-]+)+", path):
        raise ValueError("Select a dotted OpenShell field path.")
    doc = tomlkit.parse(text)
    section = doc
    *parents, key = path.split(".")
    for parent in parents:
        section = section.get(parent, {})
    if key in section:
        del section[key]
    return tomlkit.dumps(doc)


def compose(root: Path, text: str, pin: dict, telemetry=False):
    gateway = tomllib.loads(text)["openshell"]["gateway"]
    data = (root / "gateway").as_posix()
    volumes = [{"type": "bind", "source": data, "target": data}]
    base = {"image": pin["images"]["gateway"], "user": "0", "volumes": volumes}
    ports = []
    for key in ("bind_address", "health_bind_address", "metrics_bind_address"):
        if key in gateway:
            port = int(gateway[key].rsplit(":", 1)[1])
            ports.append(f"127.0.0.1:{port}:{port}")
    doc = {"services": {
        "init": {**base, "command": ["generate-certs", "--output-dir", f"{data}/tls", "--server-san", "host.openshell.internal"],
                 "environment": {"HOME": data, "XDG_CONFIG_HOME": f"{data}/bootstrap"}},
        "gateway": {**base, "restart": "unless-stopped", "command": [],
                    "depends_on": {"init": {"condition": "service_completed_successfully"}},
                    "ports": ports,
                    "labels": {"io.omosh.managed": "true"},
                    "volumes": volumes + [
                        {"type": "bind", "source": "/var/run/docker.sock", "target": "/var/run/docker.sock"},
                        {"type": "bind", "source": (root / "gateway.toml").as_posix(), "target": "/etc/openshell/gateway.toml", "read_only": True}],
                    "environment": {"OPENSHELL_GATEWAY_CONFIG": "/etc/openshell/gateway.toml",
                                    "OPENSHELL_DB_URL": f"sqlite:{data}/gateway.db?mode=rwc",
                                    "OPENSHELL_LOCAL_TLS_DIR": f"{data}/tls", "XDG_DATA_HOME": data, "HOME": data,
                                    "OPENSHELL_TELEMETRY_ENABLED": str(telemetry).lower()}},
    }}
    def literal(value):
        # Compose interpolates dollar signs even in YAML-quoted paths/values.
        if isinstance(value, str):
            return value.replace("$", "$$")
        if isinstance(value, list):
            return [literal(item) for item in value]
        if isinstance(value, dict):
            return {key: literal(item) for key, item in value.items()}
        return value
    return yaml.safe_dump(literal(doc), sort_keys=False)


def ensure_config(state):
    path = state.root / "gateway.toml"
    if not path.exists():
        private_write(path, default_config(state.root, state.get("config", "pin", load_pin())))
    return path
