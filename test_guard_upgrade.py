import importlib.util
import json
from pathlib import Path
import unittest

PACKAGE = Path(__file__).parent / "CodexWebGPT-Patcher"
spec = importlib.util.spec_from_file_location("update_bridge", PACKAGE / "payload/bin/update-bridge.py")
bridge = importlib.util.module_from_spec(spec)
spec.loader.exec_module(bridge)


class GuardUpgradeTest(unittest.TestCase):
    def test_existing_guard_updates_its_path_without_duplicating_calls(self):
        old = 'path.join(os.homedir(), ".codex-chatgpt-web", "codex-web-patcher-7a2674e73e1833ac", "bin", "update-bridge.py")'
        text = (f'const other = {old};\n// {bridge.GUARD_MARKER}\n'
                f'function hardeningGuard(version) {{\n  const script = {old};\n}}\n'
                'hardeningGuard(candidate.version);\nhardeningGuard(available.version);\n')
        updated, ok = bridge.inject_guard(text)
        state = json.loads((PACKAGE / "payload.json").read_text())["stateDirectory"]
        self.assertTrue(ok)
        self.assertIn(f'const other = {old}', updated)
        self.assertIn(f'"{state}", "bin", "update-bridge.py"', updated)
        self.assertEqual(bridge.inject_guard(updated), (updated, True))
        unknown = text.replace('function hardeningGuard(version) {', 'function changedGuard(version) {')
        self.assertEqual(bridge.inject_guard(unknown), (unknown, False))


if __name__ == "__main__":
    unittest.main()
