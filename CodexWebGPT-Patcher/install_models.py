#!/usr/bin/env python3
"""Register the local Codex Web GPT models in coding agents without changing their active model."""
import argparse
import json
import os
from pathlib import Path
import re
import shutil
import tempfile
from datetime import datetime
from urllib.parse import urlsplit

PROVIDER = "codex-web-gpt"
MODELS = (
    "chatgpt-web/gpt-6-sol",
    "chatgpt-web/gpt-6-sol-instant",
    "chatgpt-web/gpt-5.6-sol",
    "chatgpt-web/gpt-5.6-sol-instant",
)


def parse_jsonc(content):
    """Accept OpenCode's JSON-with-comments without touching quoted strings."""
    output = []
    idx = 0
    quoted = False
    while idx < len(content):
        char = content[idx]
        pair = content[idx:idx + 2]
        if quoted:
            output.append(char)
            if char == "\\" and idx + 1 < len(content):
                idx += 1
                output.append(content[idx])
            elif char == '"':
                quoted = False
        elif char == '"':
            quoted = True
            output.append(char)
        elif pair == "//":
            while idx < len(content) and content[idx] != "\n":
                idx += 1
            continue
        elif pair == "/*":
            close = content.find("*/", idx + 2)
            if close < 0:
                raise ValueError("Unclosed JSONC comment")
            output.append("\n" * content[idx:close + 2].count("\n"))
            idx = close + 2
            continue
        else:
            output.append(char)
        idx += 1
    cleaned = "".join(output)
    output = []
    quoted = False
    idx = 0
    while idx < len(cleaned):
        char = cleaned[idx]
        if char == '"':
            backslashes = 0
            while idx > backslashes and cleaned[idx - backslashes - 1] == "\\":
                backslashes += 1
            if backslashes % 2 == 0:
                quoted = not quoted
        if char == "," and not quoted and re.match(r"\s*[}\]]", cleaned[idx + 1:]):
            idx += 1
            continue
        output.append(char)
        idx += 1
    return json.loads("".join(output))


def save(path, content):
    """Atomically save one configuration, retaining a dated copy if it existed."""
    if path.is_symlink():
        raise ValueError(f"Symbolic-link configuration requires manual editing: {path}")
    path.parent.mkdir(parents=True, exist_ok=True)
    if path.exists() and path.read_text() == content:
        return False
    if path.exists():
        backup = path.with_name(path.name + ".bak-" + datetime.now().strftime("%Y%m%d-%H%M%S-%f"))
        shutil.copy2(path, backup)
        print(f"Backup: {backup}")
    fd, tmp = tempfile.mkstemp(prefix="." + path.name + "-", dir=path.parent)
    try:
        if path.exists():
            os.fchmod(fd, path.stat().st_mode & 0o777)
        with os.fdopen(fd, "w") as f:
            f.write(content)
        os.replace(tmp, path)
    finally:
        if os.path.exists(tmp):
            os.unlink(tmp)
    return True


def install_json(path, base, models, client):
    data = parse_jsonc(path.read_text()) if path.exists() and client == "opencode" else json.loads(path.read_text()) if path.exists() else {}
    if not isinstance(data, dict) or ("providers" if client == "pi" else "provider") in data and not isinstance(data["providers" if client == "pi" else "provider"], dict):
        raise ValueError(f"Invalid provider configuration: {path}")
    key = "providers" if client == "pi" else "provider"
    providers = data.setdefault(key, {})
    existing = providers.get(PROVIDER, {})
    if not isinstance(existing, dict):
        raise ValueError(f"Invalid {PROVIDER} entry: {path}")
    old_base = existing.get("baseUrl") if client == "pi" else existing.get("options", {}).get("baseURL")
    if old_base and old_base.rstrip("/") != base:
        raise ValueError(f"Existing {PROVIDER} endpoint differs in {path}: {old_base}")
    if client == "pi":
        existing.setdefault("baseUrl", base)
        existing.setdefault("api", "openai-responses")
        existing.setdefault("apiKey", "local")
        entries = existing.setdefault("models", [])
        if not isinstance(entries, list):
            raise ValueError("Pi models must be an array")
        known = {m.get("id") for m in entries if isinstance(m, dict)}
        entries.extend({"id": model} for model in models if model not in known)
    else:
        existing.setdefault("npm", "@ai-sdk/openai")  # Codex Web GPT exposes /v1/responses.
        existing.setdefault("name", "Codex Web GPT (local)")
        options = existing.setdefault("options", {})
        if not isinstance(options, dict):
            raise ValueError("OpenCode provider options must be an object")
        options.setdefault("baseURL", base)
        options.setdefault("apiKey", "local")
        entries = existing.setdefault("models", {})
        if not isinstance(entries, dict):
            raise ValueError("OpenCode models must be an object")
        for model in models:
            entries.setdefault(model, {"name": model.split("/")[-1]})
    providers[PROVIDER] = existing
    return save(path, json.dumps(data, ensure_ascii=False, indent=2) + "\n")


def section(lines, prefix, start=0, stop=None):
    """Return the span of a YAML block based only on indentation."""
    stop = len(lines) if stop is None else stop
    indent = len(prefix) - len(prefix.lstrip())
    for idx in range(start, stop):
        if lines[idx].startswith(prefix) and lines[idx].strip() == prefix.strip():
            end = idx + 1
            while end < stop and (not lines[end].strip() or lines[end].lstrip().startswith("#") or len(lines[end]) - len(lines[end].lstrip()) > indent):
                end += 1
            return idx, end
    return None


def install_hermes(path, base, models):
    lines = path.read_text().splitlines(keepends=True) if path.exists() else []
    if any("\t" in line[:len(line) - len(line.lstrip())] for line in lines):
        raise ValueError("Tab-indented Hermes YAML is not supported")
    top = section(lines, "providers:")
    if top is None:
        if lines and not lines[-1].endswith("\n"):
            lines[-1] += "\n"
        lines.extend(["providers:\n"])
        top = section(lines, "providers:")
    start, end = top
    provider = section(lines, f"  {PROVIDER}:", start + 1, end)
    if provider is None:
        block = [f"  {PROVIDER}:\n", "    name: Codex Web GPT (local)\n",
                 f"    api: {base}\n", "    transport: codex_app_server\n",
                 f"    default_model: {models[0]}\n", "    models:\n"]
        block += [f"      - {model}\n" for model in models]
        block.append("    discover_models: false\n")
        lines[end:end] = block
    else:
        a, b = provider
        entries = lines[a:b]
        for line in entries:
            if line.startswith("    api:") and line.split("#")[0].split(":", 1)[1].strip().strip("\"'").rstrip("/") != base:
                raise ValueError(f"Existing Hermes {PROVIDER} endpoint differs: {path}")
        model_line = next((i for i, line in enumerate(entries) if line.startswith("    models:")), None)
        if model_line is None:
            entries.extend(["    models:\n"] + [f"      - {m}\n" for m in models])
        else:
            if entries[model_line].strip() != "models:":
                raise ValueError("Inline Hermes models list requires manual editing")
            end_models = model_line + 1
            while end_models < len(entries) and (entries[end_models].startswith("      ") or not entries[end_models].strip()):
                end_models += 1
            existing = {m.group(1).strip().strip("\"'") for line in entries[model_line + 1:end_models] if (m := re.match(r"^\s+-\s+(.+?)\s*$", line))}
            entries[end_models:end_models] = [f"      - {m}\n" for m in models if m not in existing]
        lines[a:b] = entries
    return save(path, "".join(lines))


def main():
    parser = argparse.ArgumentParser(description="Pi / OpenCode / Hermes に Codex Web GPT のモデルを登録")
    parser.add_argument("--clients", default="pi,opencode,hermes", help="comma-separated: pi,opencode,hermes")
    parser.add_argument("--models", nargs="+", default=list(MODELS), help="model IDs to register")
    parser.add_argument("--base-url", help="Local Responses API root (default: ~/.codex-chatgpt-web/config.json)")
    args = parser.parse_args()
    clients = args.clients.split(",")
    if not clients or any(c not in ("pi", "opencode", "hermes") for c in clients):
        parser.error("--clients: choose pi,opencode,hermes")
    if not args.models or any(not re.fullmatch(r"chatgpt-web/[\w.-]+", m) for m in args.models):
        parser.error("--models must contain chatgpt-web model IDs")
    home = Path.home()
    server_config = home / ".codex-chatgpt-web/config.json"
    config = json.loads(server_config.read_text()) if server_config.exists() else {}
    base = args.base_url or f"http://{config.get('host', '127.0.0.1')}:{config.get('port', 17841)}/v1"
    url = urlsplit(base)
    if url.scheme != "http" or url.hostname not in ("localhost", "127.0.0.1", "::1") or url.path.rstrip("/") != "/v1" or url.username or url.password or url.query or url.fragment:
        parser.error("Only loopback HTTP URLs ending in /v1 are supported")
    base = base.rstrip("/")
    paths = {"pi": home / ".pi/agent/models.json",
             "opencode": home / ".config/opencode/opencode.jsonc",
             "hermes": home / ".hermes/config.yaml"}
    if not paths["opencode"].exists():
        paths["opencode"] = paths["opencode"].with_suffix(".json")
    for client in dict.fromkeys(clients):
        path = paths[client]
        try:
            changed = install_hermes(path, base, args.models) if client == "hermes" else install_json(path, base, args.models, client)
        except (OSError, ValueError, json.JSONDecodeError) as error:
            parser.exit(1, f"{client}: {error}\n")
        print(f"{client}: {'registered' if changed else 'already installed'} ({path})")
    print("Choose the model in each client's selector; existing active model is unchanged.")


if __name__ == "__main__":
    main()
