# SPDX-FileCopyrightText: 2026 CommanderTurtle
# SPDX-License-Identifier: AGPL-3.0-only
"""Generate command forms from the pinned clap declarations, never from guesses."""
import json
from pathlib import Path
import re
import sys


def kebab(value):
    return re.sub(r"(?<!^)(?=[A-Z])", "-", value).replace("_", "-").lower()


def extract(source):
    text = (source / "crates/openshell-cli/src/main.rs").read_text(encoding="utf-8")
    enums = {m[1]: m[2] for m in re.finditer(r"^enum (\w+)\s*\{(.*?)^\}", text, re.M | re.S)}
    structs = {m[1]: m[2] for m in re.finditer(r"^struct (\w+)\s*\{(.*?)^\}", text, re.M | re.S)}
    values = {name: [kebab(x) for x in re.findall(r"^    ([A-Z]\w*),\s*$", body, re.M)] for name, body in enums.items()}

    def arguments(body, indent):
        found, pending = [], []
        for line in body.splitlines():
            match = re.match(r"^" + " " * indent + r"(\w+):\s*(.+),\s*$", line)
            if match:
                attrs = "\n".join(pending)
                key, type_ = match[1], match[2]
                if "command(flatten)" in attrs:
                    found += arguments(structs.get(type_, ""), 4)
                    pending = []
                    continue
                if "command(subcommand)" in attrs:
                    pending = []
                    continue
                explicit = re.search(r'\blong\s*=\s*"([^"]+)"', attrs)
                short = re.search(r"\bshort\s*=\s*'([^']+)'", attrs)
                flag = ("--" + explicit[1] if explicit else "--" + kebab(key) if re.search(r"\blong(?:[,\)\s])", attrs)
                        else "-" + short[1] if short else "-" + key[0] if re.search(r"\bshort(?:[,\)\s])", attrs) else "")
                default = re.search(r'default_value(?:_t)?\s*=\s*([^,\)]+)', attrs)
                requires = re.findall(r'\brequires\s*=\s*"([^"]+)"', attrs)
                conflicts = re.findall(r'\bconflicts_with\s*=\s*"([^"]+)"', attrs)
                all_conflicts = re.search(r'conflicts_with_all\s*=\s*\[([^]]+)\]', attrs)
                if all_conflicts:
                    conflicts += re.findall(r'"([^"]+)"', all_conflicts[1])
                type_name = re.sub(r"^(?:Option|Vec)<(.+)>$", r"\1", type_).rsplit("::", 1)[-1]
                found.append({"key": key, "flag": flag, "type": "bool" if type_ == "bool" else "number" if re.fullmatch(r"[ui]\d+", type_name) else "string",
                              "repeat": type_.startswith("Vec<"), "last": "last = true" in attrs,
                              "required": not (type_.startswith(("Option<", "Vec<")) or default or type_ == "bool") or "required = true" in attrs,
                              "default": default[1].strip().strip('"') if default else None,
                              "help": " ".join(p.strip()[3:].strip() for p in pending if p.strip().startswith("///")),
                              "choices": values.get(type_name, []), "requires": requires, "conflicts": conflicts})
                pending = []
            elif line.strip():
                pending.append(line)
            else:
                pending = []
        return found

    result = []
    def walk(enum, prefix):
        body = enums[enum]
        matches = list(re.finditer(r"^    ([A-Z]\w*)(?:\s*\{|\((\w+)\)|,)", body, re.M))
        for index, match in enumerate(matches):
            command = [*prefix, kebab(match[1])]
            if command[0] in ("term", "completions", "ssh-proxy"):
                continue
            section = body[match.end(): matches[index + 1].start() if index + 1 < len(matches) else len(body)]
            nested = match[2] or (re.search(r"command:\s*Option<(\w+)>|command:\s*(\w+)", section))
            if nested and not isinstance(nested, str):
                nested = nested[1] or nested[2]
            if nested in enums:
                walk(nested, command)
                continue
            preceding = body[:match.start()].splitlines()
            help_lines = []
            for line in reversed(preceding):
                if line.startswith("    },") or line.strip().endswith("),") or (line.strip() and not line.startswith(("    ///", "    #["))):
                    break
                if line.startswith("    ///"):
                    help_lines.insert(0, line.strip()[3:].strip())
            result.append({"command": command, "help": " ".join(help_lines), "fields": arguments(section.split("\n    },", 1)[0], 8)})
    walk("Commands", [])
    return result
