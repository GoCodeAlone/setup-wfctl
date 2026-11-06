"""Latest-version request deadlines using the actual action step.

Synthetic transport verifies argument wiring and fail-closed output. One bounded
loopback server checks real curl's total timeout without contacting GitHub.
This does not qualify hosted Actions, DNS/TLS, or real release transport.
"""

import json
import os
from pathlib import Path
import re
import shutil
import socket
import subprocess
import sys
import tempfile
import threading
import unittest

from test_cache_integrity import ACTION, action_steps


LATEST_URL = "https://api.github.com/repos/GoCodeAlone/workflow/releases/latest"


class VersionTimeoutTests(unittest.TestCase):
    def setUp(self):
        temporary = tempfile.TemporaryDirectory(prefix="version-timeout-")
        self.addCleanup(temporary.cleanup)
        self.root = Path(temporary.name)
        self.output = self.root / "github-output"
        self.events = self.root / "curl-argv.jsonl"
        self.fetch = self.root / "fixture_curl.py"
        self.fetch.write_text(r'''import json, os, pathlib, subprocess, sys
args = sys.argv[1:]
with open(os.environ["FIXTURE_EVENTS"], "a") as stream:
    stream.write(json.dumps(args) + "\n")
url = "https://api.github.com/repos/GoCodeAlone/workflow/releases/latest"
urls = [arg for arg in args if arg.startswith("https://")]
if urls != [url] or "-o" in args:
    sys.exit(90)
mode = os.environ["FIXTURE_MODE"]
if mode == "real":
    args[args.index(url)] = os.environ["FIXTURE_LOCAL_URL"]
    sys.exit(subprocess.run([os.environ["FIXTURE_REAL_CURL"], "--noproxy", "*", *args],
                           env={"PATH": "/usr/bin:/bin", "HOME": os.environ["HOME"]}, timeout=3).returncode)
if mode == "timeout":
    sys.exit(28)
if mode == "failure":
    sys.exit(22)
if mode == "partial-timeout":
    print('{"tag_name":"v9.8.7"}')
    sys.exit(28)
if mode != "empty":
    print('{"tag_name":"v9.8.7"}')
''')

    def input_default(self, name):
        match = re.search(r"(?m)^  " + re.escape(name) + r":\n(.*?)(?=^  [\w-]+:|^runs:)", ACTION.read_text(), re.S)
        self.assertIsNotNone(match)
        default = re.search(r"(?m)^    default: '([^']*)'$", match.group(1))
        self.assertIsNotNone(default)
        return default.group(1)

    def run_step(self, version="latest", connect=None, total=None, mode="success", local_url=""):
        matches = [step for step in action_steps() if step[0] == "Determine version"]
        self.assertEqual(len(matches), 1)
        _, _, step_env, script = matches[0]
        values = {
            "inputs.version": version,
            "inputs.token": "",
            "inputs.download-connect-timeout-seconds": connect if connect is not None else self.input_default("download-connect-timeout-seconds"),
            "inputs.download-timeout-seconds": total if total is not None else self.input_default("download-timeout-seconds"),
        }
        def render(value):
            return re.sub(r"\$\{\{\s*(.*?)\s*\}\}", lambda match: values[match.group(1)], value)
        env = {
            "PATH": "/usr/bin:/bin", "HOME": str(self.root), "TMPDIR": str(self.root),
            "GITHUB_OUTPUT": str(self.output), "TEST_PYTHON": sys.executable,
            "FIXTURE_CURL": str(self.fetch), "FIXTURE_EVENTS": str(self.events),
            "FIXTURE_MODE": mode, "FIXTURE_LOCAL_URL": local_url,
            "FIXTURE_REAL_CURL": shutil.which("curl") or "",
            **{key: render(value) for key, value in step_env.items()},
        }
        script = 'curl() { "$TEST_PYTHON" -I -B -S "$FIXTURE_CURL" "$@"; }\n' + render(script)
        result = subprocess.run(["/bin/bash", "--noprofile", "--norc", "-e", "-o", "pipefail", "-c", script],
                                env=env, cwd=self.root, capture_output=True, text=True, timeout=5)
        events = [json.loads(line) for line in self.events.read_text().splitlines()] if self.events.exists() else []
        return result, events

    def assert_no_version(self, result, code):
        self.assertEqual(result.returncode, code, result.stdout + result.stderr)
        self.assertFalse(self.output.exists(), "Failed lookup published a version")
        self.assertNotIn("Resolved wfctl version:", result.stdout)

    def test_default_deadlines(self):
        result, events = self.run_step()
        self.assertEqual(result.returncode, 0, result.stderr)
        self.assertEqual(events, [["-fsSL", "--connect-timeout", "30", "--max-time", "60", "-H", "Accept: application/vnd.github+json", LATEST_URL]])
        self.assertEqual(self.output.read_text(), "version=v9.8.7\n")

    def test_custom_decimal_deadlines(self):
        result, events = self.run_step(connect="0.2", total="0.5")
        self.assertEqual(result.returncode, 0, result.stderr)
        self.assertEqual(events[0][:5], ["-fsSL", "--connect-timeout", "0.2", "--max-time", "0.5"])

    def test_timeout_values_are_single_literal_arguments(self):
        value = '1 ; $(touch timeout-injection) `touch timeout-backtick` *'
        result, events = self.run_step(connect=value, total=value)
        self.assertEqual(result.returncode, 0, result.stderr)
        self.assertEqual(events[0][:5], ["-fsSL", "--connect-timeout", value, "--max-time", value])
        self.assertFalse((self.root / "timeout-injection").exists())
        self.assertFalse((self.root / "timeout-backtick").exists())

    def test_pinned_version_makes_no_lookup(self):
        result, events = self.run_step(version="v1.2.3", mode="failure")
        self.assertEqual(result.returncode, 0, result.stderr)
        self.assertEqual(events, [])
        self.assertEqual(self.output.read_text(), "version=v1.2.3\n")

    def test_transport_failure_publishes_no_version(self):
        result, _ = self.run_step(mode="failure")
        self.assert_no_version(result, 22)

    def test_timeout_publishes_no_version(self):
        result, _ = self.run_step(mode="timeout")
        self.assert_no_version(result, 28)

    def test_timeout_with_partial_response_publishes_no_version(self):
        result, _ = self.run_step(mode="partial-timeout")
        self.assert_no_version(result, 28)

    def test_empty_response_publishes_no_version(self):
        result, _ = self.run_step(mode="empty")
        self.assert_no_version(result, 1)
        self.assertIn("Unable to resolve wfctl version", result.stderr)

    def test_real_curl_total_timeout_stops_slow_loopback_response(self):
        self.assertIsNotNone(shutil.which("curl"), "curl is required for this regression")
        connected = threading.Event()
        stop = threading.Event()
        errors = []
        with socket.socket() as listener:
            listener.bind(("127.0.0.1", 0))
            listener.listen(1)
            listener.settimeout(2)
            def serve():
                try:
                    connection, _ = listener.accept()
                    with connection:
                        connected.set()
                        if not stop.wait(0.6):
                            body = b'{"tag_name":"v9.8.7"}\n'
                            connection.sendall(b"HTTP/1.1 200 OK\r\nContent-Length: " + str(len(body)).encode() + b"\r\nConnection: close\r\n\r\n" + body)
                except Exception as error:
                    errors.append(error)
            server = threading.Thread(target=serve)
            server.start()
            try:
                result, events = self.run_step(connect="1", total="0.1", mode="real",
                                              local_url="http://127.0.0.1:" + str(listener.getsockname()[1]) + "/latest")
            finally:
                stop.set()
                server.join(timeout=3)
            self.assertFalse(server.is_alive(), "Owned loopback server did not stop")
        self.assertEqual(errors, [])
        self.assertTrue(connected.is_set(), "curl did not reach the synthetic server")
        self.assertEqual(len(events), 1)
        self.assert_no_version(result, 28)


if __name__ == "__main__":
    unittest.main()
