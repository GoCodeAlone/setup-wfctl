"""Offline shell-step controls for restored-cache integrity.

The release source and binaries are synthetic fixtures, not production artifacts.
Only curl is replaced by a fail-closed local fixture boundary. GitHub expressions
are substituted with fixed fixture values; legacy /tmp/wfctl paths are redirected
to owned fixture paths. This is not hosted Actions/cache or release authenticity
validation. The earlier existing-PATH reuse/update branch is outside this suite.
"""

import hashlib
import os
from pathlib import Path
import re
import subprocess
import sys
import tempfile
import textwrap
import unittest

ACTION = Path(os.environ.get("SETUP_WFCTL_ACTION", Path(__file__).parents[1] / "action.yml"))
VERSION = "v9.8.7"


def action_steps():
    steps = []
    for block in re.split(r"(?m)^    - name: ", ACTION.read_text())[1:]:
        name, body = block.split("\n", 1)
        scalar = re.search(r"^      run: (.+)$", body, re.M)
        if not scalar:
            continue
        script = scalar.group(1) if scalar.group(1) != "|" else textwrap.dedent(body[scalar.end() + 1:])
        condition = re.search(r"^      if: (.+)$", body, re.M)
        env = {}
        header = body[:scalar.start()]
        if "      env:\n" in header:
            for key, value in re.findall(r"^        ([A-Z_]+): (.+)$", header, re.M):
                env[key] = value
        steps.append((name, condition.group(1) if condition else None, env, script))
    return steps


class CacheIntegrityTests(unittest.TestCase):
    def setUp(self):
        self.tmp = tempfile.TemporaryDirectory(prefix="cache-integrity-")
        self.addCleanup(self.tmp.cleanup)
        self.root = Path(self.tmp.name)
        self.cache = self.root / "cache"
        self.installed = self.root / "installed"
        self.authority = self.root / "independent-release-fixture"
        self.runner_temp = self.root / "runner-temp"
        self.legacy_temp = self.root / "legacy-tmp"
        for directory in (self.cache, self.installed, self.authority, self.runner_temp, self.legacy_temp):
            directory.mkdir()
        self.events = self.root / "events"
        self.os_name = "darwin"
        self.arch = "arm64"
        self.asset = "wfctl-darwin-arm64"
        self.payload = self.binary(VERSION, self.asset)
        self.set_release(self.payload)
        self.cached(self.payload)
        self.fetch = self.root / "fetch.py"
        self.fetch.write_text('''import os, pathlib, shutil, sys
args = sys.argv[1:]
urls = [x for x in args if x.startswith("https://")]
if len(urls) != 1 or "-o" not in args:
    sys.exit(90)
base = "https://github.com/GoCodeAlone/workflow/releases/download/" + os.environ["FIXTURE_VERSION"] + "/"
url = urls[0]
if url == base + "checksums.txt":
    filename, event = "checksums.txt", "checksums"
elif url == base + os.environ["FIXTURE_ASSET"]:
    filename, event = "binary", "download"
else:
    sys.exit(91)
with open(os.environ["EXEC_LOG"], "a") as f:
    f.write(event + "\\n")
source = pathlib.Path(os.environ["FIXTURE_AUTHORITY"]) / filename
if not source.is_file():
    sys.exit(22)
destination = pathlib.Path(args[args.index("-o") + 1]).resolve()
owned_root = pathlib.Path(os.environ["FIXTURE_ROOT"]).resolve()
if not destination.is_relative_to(owned_root):
    sys.exit(92)
shutil.copyfile(source, destination)
''')

    @staticmethod
    def binary(version, asset):
        return ("#!/bin/sh\n# synthetic " + asset + "\n"
                "printf '%s\\n' execute >> \"$EXEC_LOG\"\n"
                "[ \"$1\" = version ] || exit 99\n"
                "printf '%s\\n' '" + version + "'\n").encode()

    def cached(self, payload):
        path = self.cache / "wfctl"
        path.write_bytes(payload)
        path.chmod(0o755)

    def set_release(self, payload):
        (self.authority / "binary").write_bytes(payload)
        digest = hashlib.sha256(payload).hexdigest()
        (self.authority / "checksums.txt").write_text(f"{digest}  {self.asset}\n")

    def run_action(self, cache_hit=True):
        if not cache_hit:
            (self.cache / "wfctl").unlink(missing_ok=True)
        values = {
            "steps.version.outputs.version": VERSION,
            "steps.platform.outputs.os": self.os_name,
            "steps.platform.outputs.arch": self.arch,
            "steps.cache-path.outputs.dir": str(self.cache),
            "steps.cache-path.outputs.bin": str(self.cache / "wfctl"),
            "steps.install.outputs.install_dir": str(self.installed),
            "steps.install.outputs.use_sudo": "false",
            "steps.existing.outputs.use_existing": "false",
            "steps.cache.outputs.cache-hit": str(cache_hit).lower(),
            "inputs.token": "synthetic-offline-token",
            "inputs.download-connect-timeout-seconds": "1",
            "inputs.download-timeout-seconds": "1",
        }

        def render(text):
            return re.sub(r"\$\{\{\s*(.*?)\s*\}\}", lambda match: values[match.group(1)], text)

        def enabled(condition):
            if condition is None:
                return True
            for term in condition.split(" && "):
                match = re.fullmatch(r"([\w.-]+) (==|!=) '([^']*)'", term)
                if match is None:
                    self.fail("Unsupported action condition in fixture runner: " + term)
                key, op, expected = match.groups()
                equal = values[key] == expected
                if equal != (op == "=="):
                    return False
            return True

        env = {
            "PATH": str(self.installed) + ":/usr/bin:/bin",
            "HOME": str(self.root),
            "TMPDIR": str(self.runner_temp),
            "RUNNER_TEMP": str(self.runner_temp),
            "GITHUB_OUTPUT": str(self.root / "github-output"),
            "GITHUB_PATH": str(self.root / "github-path"),
            "EXEC_LOG": str(self.events),
            "FIXTURE_VERSION": VERSION,
            "FIXTURE_ASSET": self.asset,
            "FIXTURE_AUTHORITY": str(self.authority),
            "FIXTURE_ROOT": str(self.root),
            "TEST_PYTHON": sys.executable,
            "FIXTURE_FETCH": str(self.fetch),
            "PYTHONDONTWRITEBYTECODE": "1",
        }
        selected = False
        completed = []
        result = None
        for name, condition, step_env, script in action_steps():
            if name == "Download wfctl":
                selected = True
            if not selected or not enabled(condition):
                continue
            rendered = render(script).replace("/tmp/wfctl", str(self.legacy_temp / "wfctl"))
            rendered = 'curl() { "$TEST_PYTHON" -I -B -S "$FIXTURE_FETCH" "$@"; }\n' + rendered
            result = subprocess.run(
                ["/bin/bash", "--noprofile", "--norc", "-e", "-o", "pipefail", "-c", rendered],
                env={**env, **{k: render(v) for k, v in step_env.items()}},
                cwd=self.root, capture_output=True, text=True, timeout=5,
            )
            completed.append(name)
            if result.returncode:
                break
        self.assertIsNotNone(result, "No selected shell steps executed")
        self.assertEqual(list(self.runner_temp.iterdir()), [], "Checksum scratch file leaked")
        events = self.events.read_text().splitlines() if self.events.exists() else []
        return result, events, completed

    def assert_rejected_before_execution(self, cache_hit=True, expected_events=None):
        result, events, _ = self.run_action(cache_hit)
        self.assertNotEqual(result.returncode, 0, "Bad artifact unexpectedly accepted")
        self.assertNotIn("execute", events, "Rejected artifact executed before validation")
        self.assertFalse((self.installed / "wfctl").exists(), "Rejected artifact installed")
        if expected_events is not None:
            self.assertEqual(events, expected_events)

    def test_valid_cache_verified_before_execution(self):
        source = ACTION.read_text()
        marker = "    - name: Verify cached wfctl\n"
        self.assertEqual(source.count(marker), 1)
        header = source.split(marker, 1)[1].split("      run:", 1)[0]
        self.assertEqual(re.findall(r"^      shell: (.+)$", header, re.M), ["bash"])
        self.assertEqual(re.findall(r"^      if: (.+)$", header, re.M), [
            "steps.existing.outputs.use_existing != 'true' && steps.cache.outputs.cache-hit == 'true'"
        ])
        self.assertNotIn("continue-on-error:", header, "Guard failure must stop installation")
        result, events, completed = self.run_action()
        self.assertEqual(result.returncode, 0, result.stderr)
        self.assertEqual(events, ["checksums", "execute"])
        self.assertLess(completed.index("Verify cached wfctl"), completed.index("Install wfctl"))

    def test_valid_other_platform_uses_selected_asset(self):
        self.os_name, self.arch, self.asset = "linux", "amd64", "wfctl-linux-amd64"
        self.payload = self.binary(VERSION, self.asset)
        self.set_release(self.payload)
        self.cached(self.payload)
        result, events, _ = self.run_action()
        self.assertEqual(result.returncode, 0, result.stderr)
        self.assertEqual(events, ["checksums", "execute"])

    def test_tampered_cache_is_rejected(self):
        self.cached(self.payload + b"# tampered\n")
        self.assert_rejected_before_execution()

    def test_wrong_version_cache_is_rejected_before_execution(self):
        self.cached(self.binary("v9.8.6", self.asset))
        self.assert_rejected_before_execution()

    def test_wrong_platform_cache_is_rejected(self):
        self.cached(self.binary(VERSION, "wfctl-linux-amd64"))
        self.assert_rejected_before_execution()

    def test_self_consistent_forged_cache_manifest_is_ignored(self):
        forged = self.payload + b"# forged but internally consistent cache\n"
        self.cached(forged)
        (self.cache / "checksums.txt").write_text(hashlib.sha256(forged).hexdigest() + "  " + self.asset + "\n")
        self.assert_rejected_before_execution()

    def test_missing_cached_binary_is_rejected(self):
        (self.cache / "wfctl").unlink()
        self.assert_rejected_before_execution()

    def test_empty_cached_binary_is_rejected(self):
        self.cached(b"")
        self.assert_rejected_before_execution()

    def test_symlink_cached_binary_is_rejected(self):
        target = self.root / "linked-binary"
        target.write_bytes(self.payload)
        target.chmod(0o755)
        (self.cache / "wfctl").unlink()
        (self.cache / "wfctl").symlink_to(target)
        self.assert_rejected_before_execution()

    def test_unavailable_release_checksums_fail_closed(self):
        (self.authority / "checksums.txt").unlink()
        self.assert_rejected_before_execution()

    def test_missing_asset_checksum_fails_closed(self):
        (self.authority / "checksums.txt").write_text("")
        self.assert_rejected_before_execution()

    def test_other_platform_checksum_cannot_authorize_cache(self):
        (self.authority / "checksums.txt").write_text(hashlib.sha256(self.payload).hexdigest() + "  wfctl-linux-amd64\n")
        self.assert_rejected_before_execution()

    def test_wrong_release_checksum_cannot_authorize_cache(self):
        wrong = self.binary("v9.8.6", self.asset)
        (self.authority / "checksums.txt").write_text(hashlib.sha256(wrong).hexdigest() + "  " + self.asset + "\n")
        self.assert_rejected_before_execution()

    def test_malformed_checksum_fails_closed(self):
        (self.authority / "checksums.txt").write_text("not-a-sha256  " + self.asset + "\n")
        self.assert_rejected_before_execution()

    def test_duplicate_asset_checksum_fails_closed(self):
        path = self.authority / "checksums.txt"
        path.write_text(path.read_text() * 2)
        self.assert_rejected_before_execution()

    def test_valid_cache_miss_preserves_download_verification(self):
        result, events, _ = self.run_action(cache_hit=False)
        self.assertEqual(result.returncode, 0, result.stderr)
        self.assertEqual(events, ["checksums", "download", "execute"])

    def test_tampered_cache_miss_is_rejected(self):
        (self.authority / "binary").write_bytes(self.payload + b"# bad download\n")
        self.assert_rejected_before_execution(cache_hit=False, expected_events=["checksums", "download"])

    def test_cache_miss_without_checksums_is_rejected(self):
        (self.authority / "checksums.txt").unlink()
        self.assert_rejected_before_execution(cache_hit=False, expected_events=["checksums"])

    def test_cache_miss_failed_binary_download_is_rejected(self):
        (self.authority / "binary").unlink()
        self.assert_rejected_before_execution(cache_hit=False, expected_events=["checksums", "download"])


if __name__ == "__main__":
    unittest.main()
