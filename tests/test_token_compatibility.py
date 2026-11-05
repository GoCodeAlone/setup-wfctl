"""Offline optional-header controls using the actual action's Bash scripts.

The intended regression environment uses /bin/bash 3.2. No runner input-default
semantics or hosted Actions behavior is modeled. Curl is a local fixture function
that records JSON argv; no credentials, release downloads or real binaries are
used. The existing test classes are not imported or inherited.
"""

import hashlib
import json
import os
from pathlib import Path
import re
import subprocess
import sys
import tempfile
import unittest

from test_cache_integrity import action_steps


VERSION = "v9.8.7"
ASSET = "wfctl-darwin-arm64"
LATEST_URL = "https://api.github.com/repos/GoCodeAlone/workflow/releases/latest"
RELEASE_URL = "https://github.com/GoCodeAlone/workflow/releases/download/" + VERSION
TOKEN = "synthetic-offline-token"
LITERAL_TOKEN = (
    "synthetic spaces * ? [ab] ; 'single' \"double\" \\ $HOME "
    "$(printf injected > token-injection) `printf injected > token-backtick`"
)


class TokenCompatibilityTests(unittest.TestCase):
    def setUp(self):
        temporary = tempfile.TemporaryDirectory(prefix="token-compatibility-")
        self.addCleanup(temporary.cleanup)
        self.root = Path(temporary.name)
        self.cache = self.root / "cache"
        self.authority = self.root / "synthetic-release"
        self.runner_temp = self.root / "runner-temp"
        self.legacy_temp = self.root / "legacy-tmp"
        for directory in (self.cache, self.authority, self.runner_temp, self.legacy_temp):
            directory.mkdir()
        self.events = self.root / "curl-argv.jsonl"
        self.output = self.root / "github-output"
        self.payload = b'#!/bin/sh\nprintf executed > "$EXEC_MARKER"\nexit 99\n'
        (self.cache / "wfctl").write_bytes(self.payload)
        (self.cache / "wfctl").chmod(0o755)
        (self.authority / "binary").write_bytes(self.payload)
        (self.authority / "latest").write_text(json.dumps({"tag_name": VERSION}) + "\n")
        digest = hashlib.sha256(self.payload).hexdigest()
        (self.authority / "checksums").write_text(digest + "  " + ASSET + "\n")
        self.fetch = self.root / "fixture_curl.py"
        self.fetch.write_text(r'''import json, os, pathlib, sys
args = sys.argv[1:]
with open(os.environ["FIXTURE_EVENTS"], "a") as stream:
    stream.write(json.dumps(args) + "\n")
routes = {
    os.environ["FIXTURE_LATEST_URL"]: "latest",
    os.environ["FIXTURE_RELEASE_URL"] + "/checksums.txt": "checksums",
    os.environ["FIXTURE_RELEASE_URL"] + "/" + os.environ["FIXTURE_ASSET"]: "binary",
}
urls = [value for value in args if value.startswith("https://")]
if len(urls) != 1 or urls[0] not in routes:
    sys.exit(90)
route = routes[urls[0]]
if route == os.environ["FIXTURE_FAIL"]:
    print("fixture curl: " + route + " failed", file=sys.stderr)
    sys.exit(22)
source = pathlib.Path(os.environ["FIXTURE_AUTHORITY"]) / route
if route == "latest":
    if "-o" in args:
        sys.exit(91)
    sys.stdout.buffer.write(source.read_bytes())
else:
    if args.count("-o") != 1 or args.index("-o") + 1 >= len(args):
        sys.exit(92)
    destination = pathlib.Path(args[args.index("-o") + 1]).resolve()
    root = pathlib.Path(os.environ["FIXTURE_ROOT"]).resolve()
    if not destination.is_relative_to(root):
        sys.exit(93)
    destination.write_bytes(source.read_bytes())
''')

    def run_step(self, name, token, version="latest", fail=""):
        matches = [step for step in action_steps() if step[0] == name]
        self.assertEqual(len(matches), 1, "Expected exactly one actual action step: " + name)
        _, _, step_env, script = matches[0]
        values = {
            "inputs.version": version,
            "inputs.token": token,
            "inputs.download-connect-timeout-seconds": "1",
            "inputs.download-timeout-seconds": "2",
            "steps.version.outputs.version": VERSION,
            "steps.platform.outputs.os": "darwin",
            "steps.platform.outputs.arch": "arm64",
            "steps.cache-path.outputs.dir": str(self.cache),
            "steps.cache-path.outputs.bin": str(self.cache / "wfctl"),
        }

        def render(value):
            return re.sub(r"\$\{\{\s*(.*?)\s*\}\}", lambda match: values[match.group(1)], value)

        if name == "Download wfctl":
            (self.cache / "wfctl").unlink()
        script = render(script).replace("/tmp/wfctl", str(self.legacy_temp / "wfctl"))
        script = (
            'curl() { "$TEST_PYTHON" -I -B -S "$FIXTURE_CURL" "$@"; }\n'
            'printf "%s\\n" "$BASH_VERSION" > "$FIXTURE_BASH_VERSION"\n' + script
        )
        env = {
            "PATH": "/usr/bin:/bin",
            "HOME": str(self.root),
            "TMPDIR": str(self.runner_temp),
            "RUNNER_TEMP": str(self.runner_temp),
            "GITHUB_OUTPUT": str(self.output),
            "TEST_PYTHON": sys.executable,
            "FIXTURE_CURL": str(self.fetch),
            "FIXTURE_EVENTS": str(self.events),
            "FIXTURE_AUTHORITY": str(self.authority),
            "FIXTURE_ROOT": str(self.root),
            "FIXTURE_LATEST_URL": LATEST_URL,
            "FIXTURE_RELEASE_URL": RELEASE_URL,
            "FIXTURE_ASSET": ASSET,
            "FIXTURE_FAIL": fail,
            "FIXTURE_BASH_VERSION": str(self.root / "bash-version"),
            "EXEC_MARKER": str(self.root / "binary-executed"),
        }
        result = subprocess.run(
            ["/bin/bash", "--noprofile", "--norc", "-e", "-o", "pipefail", "-c", script],
            cwd=self.root, env={**env, **{key: render(value) for key, value in step_env.items()}},
            capture_output=True, text=True, timeout=5,
        )
        self.diagnostic = "exit=" + str(result.returncode) + "\nstdout:\n" + result.stdout + "\nstderr:\n" + result.stderr
        self.assertEqual(list(self.runner_temp.iterdir()), [], self.diagnostic + "\nScratch file leaked")
        for marker in ("token-injection", "token-backtick", "binary-executed"):
            self.assertFalse((self.root / marker).exists(), self.diagnostic + "\nUnexpected execution: " + marker)
        events = [json.loads(line) for line in self.events.read_text().splitlines()] if self.events.exists() else []
        return result, events

    def assert_requests(self, events, routes, token, cached=False):
        self.assertEqual(len(events), len(routes), self.diagnostic + "\nWrong number of curl calls")
        for args, route in zip(events, routes):
            headers, rest = [], []
            index = 0
            while index < len(args):
                if args[index] == "-H":
                    self.assertLess(index + 1, len(args), self.diagnostic)
                    headers.append(args[index + 1])
                    index += 2
                else:
                    rest.append(args[index])
                    index += 1
            expected_headers = []
            if token:
                scheme = "Bearer" if route == "latest" else "token"
                expected_headers.append("Authorization: " + scheme + " " + token)
            if route == "latest":
                expected_headers.append("Accept: application/vnd.github+json")
                self.assertEqual(rest, ["-fsSL", LATEST_URL], self.diagnostic)
            else:
                self.assertEqual(rest[:8], [
                    "--fail", "--show-error", "--silent", "--location",
                    "--connect-timeout", "1", "--max-time", "2",
                ], self.diagnostic)
                url = RELEASE_URL + "/" + ("checksums.txt" if route == "checksums" else ASSET)
                self.assertEqual(len(rest), 11, self.diagnostic)
                self.assertEqual(rest[8:10], [url, "-o"], self.diagnostic)
                destination = Path(rest[10])
                if cached:
                    self.assertEqual(route, "checksums", self.diagnostic)
                    self.assertEqual(destination.parent, self.runner_temp, self.diagnostic)
                    self.assertTrue(destination.name.startswith("setup-wfctl-checksums."), self.diagnostic)
                else:
                    filename = "checksums.txt" if route == "checksums" else "wfctl"
                    self.assertEqual(destination.name, filename, self.diagnostic)
                    self.assertEqual(destination.parent.parent, self.runner_temp, self.diagnostic)
                    self.assertTrue(destination.parent.name.startswith("setup-wfctl-download."), self.diagnostic)
            self.assertCountEqual(headers, expected_headers, self.diagnostic)

    def assert_latest(self, token):
        result, events = self.run_step("Determine version", token)
        self.assertEqual(result.returncode, 0, self.diagnostic)
        self.assert_requests(events, ["latest"], token)
        self.assertEqual(self.output.read_text(), "version=" + VERSION + "\n", self.diagnostic)

    def assert_download(self, token):
        result, events = self.run_step("Download wfctl", token)
        self.assertEqual(result.returncode, 0, self.diagnostic)
        self.assert_requests(events, ["checksums", "binary"], token)
        self.assertEqual((self.cache / "wfctl").read_bytes(), self.payload, self.diagnostic)

    def assert_cached(self, token):
        result, events = self.run_step("Verify cached wfctl", token)
        self.assertEqual(result.returncode, 0, self.diagnostic)
        self.assert_requests(events, ["checksums"], token, cached=True)
        self.assertEqual((self.cache / "wfctl").read_bytes(), self.payload, self.diagnostic)

    def assert_rejected(self, name, routes, diagnostic, fail="", cached=False):
        result, events = self.run_step(name, "", fail=fail)
        self.assertNotEqual(result.returncode, 0, self.diagnostic)
        # An earlier empty-array shell failure is not proof of the intended rejection.
        self.assert_requests(events, routes, "", cached=cached)
        self.assertIn(diagnostic, result.stderr, self.diagnostic)
        if name == "Download wfctl":
            self.assertFalse((self.cache / "wfctl").exists(), self.diagnostic)
        if name == "Determine version":
            self.assertFalse(self.output.exists(), self.diagnostic)

    def test_latest_empty_token(self):
        self.assert_latest("")

    def test_latest_nonempty_token(self):
        self.assert_latest(TOKEN)

    def test_latest_literal_token(self):
        self.assert_latest(LITERAL_TOKEN)

    def test_download_empty_token(self):
        self.assert_download("")

    def test_download_nonempty_token(self):
        self.assert_download(TOKEN)

    def test_download_literal_token(self):
        self.assert_download(LITERAL_TOKEN)

    def test_cache_hit_empty_token(self):
        self.assert_cached("")

    def test_cache_hit_nonempty_token(self):
        self.assert_cached(TOKEN)

    def test_cache_hit_literal_token(self):
        self.assert_cached(LITERAL_TOKEN)

    def test_pinned_version_empty_token_makes_no_request(self):
        result, events = self.run_step("Determine version", "", version=VERSION)
        self.assertEqual(result.returncode, 0, self.diagnostic)
        self.assert_requests(events, [], "")
        self.assertEqual(self.output.read_text(), "version=" + VERSION + "\n", self.diagnostic)

    def test_pinned_version_nonempty_token_makes_no_request(self):
        result, events = self.run_step("Determine version", TOKEN, version=VERSION)
        self.assertEqual(result.returncode, 0, self.diagnostic)
        self.assert_requests(events, [], TOKEN)
        self.assertEqual(self.output.read_text(), "version=" + VERSION + "\n", self.diagnostic)

    def test_empty_token_latest_without_tag_is_rejected(self):
        (self.authority / "latest").write_text("{}\n")
        self.assert_rejected("Determine version", ["latest"], "Unable to resolve wfctl version")

    def test_empty_token_latest_api_failure_is_rejected(self):
        self.assert_rejected("Determine version", ["latest"], "fixture curl: latest failed", fail="latest")

    def test_empty_token_checksum_fetch_failure_is_rejected(self):
        self.assert_rejected("Download wfctl", ["checksums"], "fixture curl: checksums failed", fail="checksums")

    def test_empty_token_binary_fetch_failure_is_rejected(self):
        self.assert_rejected("Download wfctl", ["checksums", "binary"], "fixture curl: binary failed", fail="binary")

    def test_empty_token_download_checksum_mismatch_is_rejected(self):
        (self.authority / "binary").write_bytes(self.payload + b"# mismatched\n")
        self.assert_rejected("Download wfctl", ["checksums", "binary"], "wfctl checksum mismatch")

    def test_empty_token_cache_manifest_failure_is_rejected(self):
        self.assert_rejected("Verify cached wfctl", ["checksums"], "fixture curl: checksums failed", fail="checksums", cached=True)

    def test_empty_token_cache_checksum_mismatch_is_rejected(self):
        (self.cache / "wfctl").write_bytes(self.payload + b"# mismatched\n")
        self.assert_rejected("Verify cached wfctl", ["checksums"], "Cached wfctl checksum mismatch", cached=True)


if __name__ == "__main__":
    unittest.main()
