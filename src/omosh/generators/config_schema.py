# SPDX-FileCopyrightText: 2026 CommanderTurtle
# SPDX-License-Identifier: AGPL-3.0-only
"""Index public serde config fields from the pinned Rust source (data only)."""
import json
from pathlib import Path
import re
import sys
import tomllib


def extract(source: Path, commit: str):
    structs, enums = {}, {}
    crates = ["openshell-server", "openshell-core", "openshell-driver-docker", "openshell-driver-podman", "openshell-driver-vm", "openshell-driver-kubernetes"]
    for crate in crates:
        for file in sorted((source / "crates" / crate / "src").rglob("*.rs")):
            text = file.read_text(encoding="utf-8")
            for match in re.finditer(r"(?:pub )?struct (\w+)\s*\{(.*?)\n\}", text, re.S):
                members, pending = [], []
                for line in match[2].splitlines():
                    field = re.match(r"\s*(?:pub )?(\w+):\s*(.*?),?\s*$", line)
                    if field:
                        attrs = "\n".join(pending)
                        if "#[serde(skip)]" not in attrs:
                            members.append({"name": field[1], "rust_type": field[2].rstrip(","),
                                "help": " ".join(p.strip()[3:].strip() for p in pending if p.strip().startswith("///")),
                                "flatten": "serde(flatten)" in attrs, "required": "Option<" not in field[2] and "serde(default" not in attrs})
                        pending = []
                    elif line.strip():
                        pending.append(line)
                    else:
                        pending = []
                structs.setdefault(match[1], {"fields": members, "file": file.relative_to(source).as_posix()})
            for match in re.finditer(r"pub enum (\w+)\s*\{(.*?)\n\}", text, re.S):
                values = re.findall(r"^    ([A-Z]\w*),\s*$", match[2], re.M)
                enums.setdefault(match[1], [re.sub(r"(?<!^)(?=[A-Z])", "_", v).lower() for v in values])
    result = []
    aliases = {"OtlpConfig": "RawOtlpConfig", "OcsfLogConfig": "RawOcsfLogConfig", "ExtensionBindingPolicy": "GatewayInterceptorBindingPolicy"}

    def describe(type_, visited=()):
        type_ = type_.strip()
        if type_.startswith("Option<"):
            return {**describe(type_[7:-1], visited), "optional": True}
        if type_.startswith("Vec<"):
            return {"type": "array", "items": describe(type_[4:-1], visited)}
        if re.match(r"(?:BTreeMap|HashMap)<", type_):
            depth = 0
            for index, char in enumerate(type_):
                depth += (char == "<") - (char == ">")
                if char == "," and depth == 1:
                    return {"type": "object", "additional": describe(type_[index + 1:-1], visited)}
        nested = type_.rsplit("::", 1)[-1]
        nested = aliases.get(nested, nested)
        if nested == "GatewayProviderProfileSourceConfig":
            return {"type": "object", "discriminator": "type", "properties": {"type": {"type": "string", "choices": ["user", "interceptor"], "required": True}},
                    "variants": {"user": {}, "interceptor": {"name": {"type": "string", "required": True}}}}
        if nested == "SandboxTransport":
            return {"type": "object", "discriminator": "kind", "properties": {"kind": {"type": "string", "choices": ["unix", "tcp", "vsock"], "required": True}},
                    "variants": {"unix": {"socket_path": {"type": "string", "required": True}},
                                 "tcp": {"authority": {"type": "string", "required": True}, "addresses": {"type": "array", "items": {"type": "string"}, "required": True}},
                                 "vsock": {"guest_cid": {"type": "integer", "minimum": 3, "required": True}, "port": {"type": "integer", "minimum": 1, "required": True}}}}
        if nested in structs and nested not in visited and len(visited) < 6:
            properties = {}
            for member in structs[nested]["fields"]:
                child = {**describe(member["rust_type"], (*visited, nested)), "help": member["help"], "required": member["required"]}
                if member["flatten"]:
                    properties.update(child.get("properties", {}))
                else:
                    properties[member["name"]] = child
            return {"type": "object", "properties": properties}
        if nested in enums:
            return {"type": "string", "choices": enums[nested]}
        if nested == "bool":
            return {"type": "boolean"}
        if re.fullmatch(r"(?:[ui]\d+|usize|NonZero\w+)", nested):
            return {"type": "integer", **({"minimum": 1} if nested.startswith("NonZero") else {"minimum": 0} if nested.startswith(("u", "NonZeroU")) else {})}
        if nested in ("Table", "Value"):
            return {"type": "object"}
        return {"type": "string"}

    def walk(struct_name, prefix, visited=()):
        struct_name = aliases.get(struct_name, struct_name)
        if struct_name in visited or struct_name not in structs:
            return
        definition = structs[struct_name]
        for item in definition["fields"]:
            if item["name"] in ("database_url", "guest_tls_cert", "guest_tls_key"):
                continue
            if prefix.endswith("drivers.docker") and item["name"] == "guest_tls_ca":
                continue
            type_ = item["rust_type"]
            path = prefix if item["flatten"] else prefix + "." + item["name"]
            clean = re.sub(r"Option<(.+)>", r"\1", type_)
            nested = clean.rsplit("::", 1)[-1]
            if nested in structs and not clean.startswith(("Vec", "BTree", "Hash")):
                walk(nested, path, (*visited, struct_name))
            else:
                kind = "bool" if clean == "bool" else "number" if re.fullmatch(r"(?:[ui]\d+|usize|NonZero\w+)", nested) else "json" if any(x in clean for x in ("Vec<", "Map<", "Table", "Value")) else "string"
                result.append({"path": path, "type": kind, "file": nested in ("PathBuf", "Path"), "optional": type_.startswith("Option<"), "choices": enums.get(nested, []),
                               "help": item["help"], "structure": describe(type_), "source": f"https://github.com/NVIDIA/OpenShell/blob/{commit}/{definition['file']}"})
    walk("GatewayFileSection", "openshell.gateway")
    walk("SupervisorFileSection", "openshell.supervisor")
    for driver, type_ in [("docker", "DockerComputeConfig"), ("podman", "PodmanComputeConfig"), ("kubernetes", "KubernetesComputeConfig"), ("vm", "VmDriverConfig")]:
        walk(type_, f"openshell.drivers.{driver}")
        result.append({"path": f"openshell.drivers.{driver}.isolation_backend", "type": "string", "optional": True, "choices": [],
                       "help": "Select an operator-registered isolation backend. Omit for the built-in sandbox backend.",
                       "source": f"https://github.com/NVIDIA/OpenShell/blob/{commit}/crates/openshell-server/src/config_file.rs"})
    examples = {}
    def flatten(data, prefix=""):
        for key, value in data.items():
            path = prefix + "." + key if prefix else key
            if isinstance(value, dict):
                flatten(value, path)
            else:
                examples.setdefault(path, value)
    for file in sorted((source / "docs").rglob("*.mdx")):
        for block in re.findall(r"```toml[^\n]*\n(.*?)\n```", file.read_text(encoding="utf-8"), re.S):
            try:
                flatten(tomllib.loads(block))
            except tomllib.TOMLDecodeError:
                pass
    for item in result:
        if item["path"] in examples:
            item["example"] = examples[item["path"]]
        path = item["path"]
        item["manual"] = ("how-it-works/gateways/authentication" if ".oidc." in path or ".mtls_auth." in path else
                          "extensibility/gateway-interceptors" if ".interceptors" in path else
                          "extensibility/supervisor-middleware/configure" if path.endswith("supervisor.middleware") else
                          "extensibility/isolation-backends" if "isolation_backend" in path else
                          "how-it-works/providers/profiles" if "provider_profile_sources" in path else
                          "how-it-works/sandboxes/runtimes" if ".drivers." in path else "how-it-works/gateways/configuration")
    result.sort(key=lambda i: (not i["path"].startswith("openshell.gateway"), not i["path"].startswith("openshell.drivers.docker"), i["path"]))
    return result
