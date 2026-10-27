"""Smoke tests for model installation without changing real user settings."""
import json
import os
from pathlib import Path
import subprocess
import sys
import tempfile

SCRIPT = Path(__file__).parent / "CodexWebGPT-Patcher/install_models.py"

with tempfile.TemporaryDirectory() as tmp:
    home = Path(tmp)
    pi = home / ".pi/agent/models.json"
    oc = home / ".config/opencode/opencode.jsonc"
    hermes = home / ".hermes/config.yaml"
    server = home / ".codex-chatgpt-web/config.json"
    for path in (pi, oc, hermes, server):
        path.parent.mkdir(parents=True, exist_ok=True)
    pi.write_text('{"providers":{"other":{"apiKey":"preserve"}}}')
    oc.write_text('{ // comment\n "other":"keep://example/path", "provider":{"other":{}}, }')
    hermes.write_text("model:\n  default: preserved\nproviders:\n  other:\n    api: http://127.0.0.1:1/v1\n    models:\n      - other/model\n  codex-web-gpt:\n    api: http://127.0.0.1:17841/v1\n    transport: codex_app_server\n    models:\n      - chatgpt-web/gpt-6-sol\n  another:\n    models:\n      - keep/this\ndisplay:\n  compact: true\n")
    server.write_text('{"host":"127.0.0.1","port":17841}')
    env = dict(os.environ, HOME=str(home))

    def run(*arguments, ok=True):
        result = subprocess.run([sys.executable, str(SCRIPT), *arguments], env=env, text=True, capture_output=True)
        assert (result.returncode == 0) == ok, result.stderr
        return result

    run()
    pi_data = json.loads(pi.read_text())
    oc_data = json.loads(oc.read_text())
    assert pi_data["providers"]["other"]["apiKey"] == "preserve"
    assert pi_data["providers"]["codex-web-gpt"]["api"] == "openai-responses"
    assert len(pi_data["providers"]["codex-web-gpt"]["models"]) == 4
    assert oc_data["other"] == "keep://example/path"
    assert oc_data["provider"]["codex-web-gpt"]["npm"] == "@ai-sdk/openai"
    assert len(oc_data["provider"]["codex-web-gpt"]["models"]) == 4
    assert hermes.read_text().count("  another:") == 1
    assert hermes.read_text().count("      - chatgpt-web/gpt-6-sol-instant") == 1
    assert hermes.read_text().endswith("display:\n  compact: true\n")
    snapshot = [p.read_bytes() for p in (pi, oc, hermes)]
    run()
    assert snapshot == [p.read_bytes() for p in (pi, oc, hermes)]
    assert [len(list(p.parent.glob(p.name + ".bak-*"))) for p in (pi, oc, hermes)] == [1, 1, 1]
    run("--clients", "pi", "--base-url", "https://example.com/v1", ok=False)
    assert snapshot[0] == pi.read_bytes()
    run("--clients", "pi", "--base-url", "http://127.0.0.1:12345/v1", ok=False)
    assert snapshot[0] == pi.read_bytes()
    run("--clients", "unknown", ok=False)
    print("MODEL_INSTALL_TEST_OK")
