"""Actual download-step filesystem checks with synthetic, fail-closed transport.

No network, credentials, real wfctl binaries, or shared /tmp files are used.
Legacy baseline paths are redirected only to an owned temporary directory.
"""
import hashlib
import json
import os
from pathlib import Path
import re
import signal
import stat
import subprocess
import sys
import tempfile
import unittest

from test_cache_integrity import action_steps


class DownloadIsolationTests(unittest.TestCase):
    def setUp(self):
        self.temporary = tempfile.TemporaryDirectory(prefix="download-isolation-")
        self.addCleanup(self.temporary.cleanup)
        self.root = Path(self.temporary.name)
        self.runner_temp = self.root / "runner temp"
        self.legacy = self.root / "legacy-tmp"
        self.runner_temp.mkdir()
        self.legacy.mkdir()
        self.fetch = self.root / "fixture_curl.py"
        self.fetch.write_text(r'''import hashlib, json, os, pathlib, stat, sys, time
args = sys.argv[1:]
urls = [arg for arg in args if arg.startswith("https://")]
base = "https://github.com/GoCodeAlone/workflow/releases/download/v9.8.7/"
allowed = {base + "checksums.txt": "checksums", base + "wfctl-darwin-arm64": "binary"}
if len(urls) != 1 or urls[0] not in allowed or args.count("-o") != 1:
    sys.exit(90)
kind = allowed[urls[0]]
destination = pathlib.Path(args[args.index("-o") + 1])
root = pathlib.Path(os.environ["FIXTURE_ROOT"]).resolve()
if not destination.resolve().is_relative_to(root):
    sys.exit(91)
with open(os.environ["FIXTURE_LOG"], "a") as stream:
    stream.write(json.dumps({"kind": kind, "path": str(destination), "mode": stat.S_IMODE(destination.parent.stat().st_mode), "args": args}) + "\n")
mode = os.environ["FIXTURE_MODE"]
payload = ("synthetic payload " + os.environ["FIXTURE_LABEL"]).encode()
if kind == "checksums":
    if mode == "checksum-fetch-failure":
        sys.exit(22)
    digest = hashlib.sha256(payload).hexdigest()
    if mode == "checksum-mismatch":
        digest = "0" * 64
    asset = "other-asset" if mode == "missing-checksum" else "wfctl-darwin-arm64"
    destination.write_text(digest + "  " + asset + "\n")
else:
    destination.write_bytes(payload)
    if mode == "binary-partial-failure":
        sys.exit(22)
    if mode == "overlap":
        label = os.environ["FIXTURE_LABEL"]
        (root / (label + ".ready")).touch()
        other = root / (("b" if label == "a" else "a") + ".ready")
        deadline = time.monotonic() + 2
        while not other.exists():
            if time.monotonic() >= deadline:
                sys.exit(92)
            time.sleep(0.01)
''')

    def command(self, label="a", mode="success", hash_tool="shasum", extra="", runner_temp=True):
        matches = [step for step in action_steps() if step[0] == "Download wfctl"]
        self.assertEqual(len(matches), 1)
        _, _, step_env, script = matches[0]
        cache = self.root / ("cache-" + label)
        values = {
            "steps.version.outputs.version": "v9.8.7",
            "steps.platform.outputs.os": "darwin",
            "steps.platform.outputs.arch": "arm64",
            "steps.cache-path.outputs.dir": str(cache),
            "steps.cache-path.outputs.bin": str(cache / "wfctl"),
            "inputs.token": "synthetic-offline-token",
            "inputs.download-connect-timeout-seconds": "1",
            "inputs.download-timeout-seconds": "2",
        }
        def render(value):
            return re.sub(r"\$\{\{\s*(.*?)\s*\}\}", lambda m: values[m.group(1)], value)
        script = render(script).replace("/tmp/wfctl", str(self.legacy / "wfctl"))
        prefix = 'curl() { "$TEST_PYTHON" -I -B -S "$FIXTURE_CURL" "$@"; }\n'
        # Exercise both real digest branches without requiring extra packages.
        if hash_tool == "sha256sum":
            prefix += 'sha256sum() { /usr/bin/shasum -a 256 "$@"; }\n'
        else:
            prefix += 'command() { if [ "$1" = -v ] && [ "$2" = sha256sum ]; then return 1; fi; builtin command "$@"; }\n'
        env = {
            "PATH": "/usr/bin:/bin", "HOME": str(self.root), "TMPDIR": str(self.runner_temp),
            "TEST_PYTHON": sys.executable, "FIXTURE_CURL": str(self.fetch),
            "FIXTURE_ROOT": str(self.root), "FIXTURE_LOG": str(self.root / (label + ".jsonl")),
            "FIXTURE_MODE": mode, "FIXTURE_LABEL": label,
            **{key: render(value) for key, value in step_env.items()},
        }
        if runner_temp:
            env["RUNNER_TEMP"] = str(self.runner_temp)
        return ["/bin/bash", "--noprofile", "--norc", "-e", "-o", "pipefail", "-c", prefix + extra + script], env

    def run_step(self, **kwargs):
        args, env = self.command(**kwargs)
        child = subprocess.Popen(args, env=env, cwd=self.root, stdout=subprocess.PIPE,
                                 stderr=subprocess.PIPE, text=True, start_new_session=True)
        try:
            stdout, stderr = child.communicate(timeout=5)
            result = subprocess.CompletedProcess(args, child.returncode, stdout, stderr)
        finally:
            if child.poll() is None:
                os.killpg(child.pid, signal.SIGKILL)
            child.communicate(timeout=5)
        self.assertEqual(list(self.runner_temp.iterdir()), [], "Owned temporary directory leaked")
        return result

    def events(self, label="a"):
        log = self.root / (label + ".jsonl")
        return [json.loads(line) for line in log.read_text().splitlines()] if log.exists() else []

    def assert_isolated(self, label="a"):
        events = self.events(label)
        self.assertTrue(events)
        directories = {Path(event["path"]).parent for event in events}
        self.assertEqual(len(directories), 1)
        directory = directories.pop()
        self.assertEqual(directory.parent, self.runner_temp, "Download escaped the invocation's runner temporary directory")
        self.assertTrue(directory.name.startswith("setup-wfctl-download."))
        self.assertEqual({event["mode"] for event in events}, {0o700})
        self.assertFalse(directory.exists(), "Download directory was not cleaned")
        return directory

    def assert_success(self, result, label="a"):
        self.assertEqual(result.returncode, 0, result.stdout + result.stderr)
        output = self.root / ("cache-" + label) / "wfctl"
        self.assertEqual(output.read_bytes(), ("synthetic payload " + label).encode())
        self.assertTrue(output.stat().st_mode & stat.S_IXUSR)
        self.assertFalse(output.is_symlink())
        self.assert_isolated(label)

    def assert_failure(self, mode, expected_code, expected_events, diagnostic="", extra=""):
        result = self.run_step(mode=mode, extra=extra)
        self.assertEqual(result.returncode, expected_code, result.stdout + result.stderr)
        self.assertEqual([event["kind"] for event in self.events()], expected_events)
        if diagnostic:
            self.assertIn(diagnostic, result.stderr)
        self.assertFalse((self.root / "cache-a" / "wfctl").exists())
        self.assert_isolated()

    def test_success_and_shasum_fallback(self):
        self.assert_success(self.run_step())

    def test_sha256sum_branch(self):
        self.assert_success(self.run_step(hash_tool="sha256sum"))

    def test_preexisting_legacy_symlinks_are_untouched(self):
        for name in ("wfctl", "wfctl-checksums.txt"):
            victim = self.root / (name + ".sentinel")
            victim.write_bytes(b"preserve existing owned fixture")
            (self.legacy / name).symlink_to(victim)
        result = self.run_step()
        for name in ("wfctl", "wfctl-checksums.txt"):
            self.assertTrue((self.legacy / name).is_symlink())
            self.assertEqual((self.root / (name + ".sentinel")).read_bytes(), b"preserve existing owned fixture")
        self.assert_success(result)

    def test_checksum_fetch_failure_cleans(self):
        self.assert_failure("checksum-fetch-failure", 22, ["checksums"])

    def test_missing_checksum_cleans(self):
        self.assert_failure("missing-checksum", 1, ["checksums"], "does not list wfctl-darwin-arm64")

    def test_partial_binary_fetch_failure_cleans(self):
        self.assert_failure("binary-partial-failure", 22, ["checksums", "binary"])

    def test_checksum_mismatch_cleans(self):
        self.assert_failure("checksum-mismatch", 1, ["checksums", "binary"], "wfctl checksum mismatch")

    def test_move_failure_cleans(self):
        self.assert_failure("success", 73, ["checksums", "binary"], extra="mv() { return 73; }\n")

    def test_sequential_downloads_have_unique_directories(self):
        self.assert_success(self.run_step(label="a"), "a")
        first = self.assert_isolated("a")
        self.assert_success(self.run_step(label="b"), "b")
        self.assertNotEqual(first, self.assert_isolated("b"))

    def test_missing_runner_temp_fails_before_download(self):
        result = self.run_step(runner_temp=False)
        self.assertNotEqual(result.returncode, 0)
        self.assertEqual(self.events(), [])
        self.assertFalse((self.root / "cache-a").exists())

    def test_mktemp_failure_fails_before_download(self):
        result = self.run_step(extra="mktemp() { return 74; }\n")
        self.assertNotEqual(result.returncode, 0)
        self.assertEqual(self.events(), [])
        self.assertFalse((self.root / "cache-a").exists())

    def test_overlapping_downloads_are_independent(self):
        children = []
        try:
            for label in ("a", "b"):
                args, env = self.command(label=label, mode="overlap")
                children.append((label, subprocess.Popen(args, env=env, cwd=self.root, stdout=subprocess.PIPE, stderr=subprocess.PIPE, text=True, start_new_session=True)))
            results = []
            for label, child in children:
                stdout, stderr = child.communicate(timeout=5)
                results.append((label, subprocess.CompletedProcess(child.args, child.returncode, stdout, stderr)))
            for label, result in results:
                self.assert_success(result, label)
            self.assertNotEqual(self.assert_isolated("a"), self.assert_isolated("b"))
            self.assertEqual(list(self.runner_temp.iterdir()), [])
        finally:
            for _, child in children:
                if child.poll() is None:
                    os.killpg(child.pid, signal.SIGKILL)
                child.communicate(timeout=5)


if __name__ == "__main__":
    unittest.main()
