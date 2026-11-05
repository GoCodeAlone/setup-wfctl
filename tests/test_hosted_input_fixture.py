"""Local controls for the hosted-check oracle, not hosted Actions validation."""

import json
import os
from pathlib import Path
import subprocess
import sys
import tempfile
import unittest


HELPER = Path(__file__).with_name("hosted_input_fixture.py")
TOKEN = "synthetic-token with spaces;$(never-executed)"
URL = "https://api.github.com/repos/GoCodeAlone/workflow/releases/latest"


class HostedInputFixtureTests(unittest.TestCase):
    peak_fixture_bytes = 0

    def setUp(self):
        self.tmp = tempfile.TemporaryDirectory(prefix="hosted-input-oracle-")
        self.root = Path(self.tmp.name)
        self.addCleanup(self.cleanup_storage)
        self.env = dict(os.environ, RUNNER_TEMP=str(self.root), RUNNER_TOOL_CACHE=str(self.root / "toolcache"),
                        GITHUB_ENV=str(self.root / "github-env"), GITHUB_PATH=str(self.root / "github-path"),
                        GITHUB_RUN_ID="123456", GITHUB_RUN_ATTEMPT="1", PYTHONDONTWRITEBYTECODE="1",
                        EXPECTED_DEFAULT_TOKEN=TOKEN, INPUT_MODE="default")
        self.call("init", expected=0)
        self.env.update(dict(line.split("=", 1) for line in (self.root / "github-env").read_text().splitlines()))
        self.owned = Path(self.env["HOSTED_INPUT_ROOT"])
        self.env["PATH"] = str(self.owned / "bin") + os.pathsep + os.environ["PATH"]

    def cleanup_storage(self):
        self.tmp.cleanup()
        self.assertFalse(self.root.exists(), "Local fixture storage remained after cleanup")

    @classmethod
    def tearDownClass(cls):
        print("Sampled local fixture peak bytes:", cls.peak_fixture_bytes)

    def run_command(self, command, expected=None):
        result = subprocess.run(command, env=self.env,
                                capture_output=True, text=True, timeout=3)
        self.assertNotIn(TOKEN, result.stdout + result.stderr)
        fixture_bytes = 0
        for path in self.root.rglob("*"):
            if path.is_file() and not path.is_symlink():
                data = path.read_bytes()
                fixture_bytes += len(data)
                self.assertNotIn(TOKEN.encode(), data, "Credential appeared in fixture storage")
        self.__class__.peak_fixture_bytes = max(self.peak_fixture_bytes, fixture_bytes)
        self.assertLess(fixture_bytes, 16 * 1024 * 1024)
        if expected is not None:
            self.assertEqual(result.returncode, expected, result.stderr)
        return result

    def call(self, *args, expected=None):
        return self.run_command([sys.executable, "-I", "-B", str(HELPER), *args], expected)

    def request(self, token=TOKEN, url=URL):
        args = ["-fsSL"]
        if token is not None:
            args += ["-H", "Authorization: Bearer " + token]
        return args + ["-H", "Accept: application/vnd.github+json", url]

    def successful_calls(self):
        self.call("curl", *self.request(None if self.env["INPUT_MODE"] == "empty" else TOKEN), expected=0)
        self.call("wfctl", "version", expected=0)
        self.call("wfctl", "version", expected=0)

    def test_default_header_and_exact_success_receipt(self):
        self.successful_calls()
        result = self.call("verify", "default", "success", expected=0)
        self.assertEqual(json.loads(result.stdout)["events"], ["latest_response", "wfctl_version", "wfctl_version"])

    def test_empty_header_and_exact_success_receipt(self):
        self.env["INPUT_MODE"] = "empty"
        self.successful_calls()
        self.call("verify", "empty", "success", expected=0)

    def test_created_wrappers_preserve_literal_arguments(self):
        result = self.run_command(["curl", *self.request()], expected=0)
        self.assertEqual(json.loads(result.stdout)["tag_name"], self.env["HOSTED_INPUT_VERSION"])
        for _ in range(2):
            result = self.run_command(["wfctl", "version"], expected=0)
            self.assertEqual(result.stdout.strip(), self.env["HOSTED_INPUT_VERSION"])
        self.call("verify", "default", "success", expected=0)

    def test_exact_default_header_is_deliberately_rejected(self):
        self.env["INPUT_MODE"] = "rejected"
        self.call("curl", *self.request(), expected=22)
        self.call("verify", "rejected", "failure", expected=0)

    def test_wrong_token_does_not_satisfy_negative_control(self):
        self.env["INPUT_MODE"] = "rejected"
        self.call("curl", *self.request("wrong-token"), expected=90)
        self.assertNotEqual(self.call("verify", "rejected", "failure").returncode, 0)

    def test_wrong_url_cannot_satisfy_negative_control(self):
        self.env["INPUT_MODE"] = "rejected"
        self.call("curl", *self.request(url="https://example.invalid/"), expected=90)
        self.assertNotEqual(self.call("verify", "rejected", "failure").returncode, 0)

    def test_empty_input_rejects_any_authorization(self):
        self.env["INPUT_MODE"] = "empty"
        self.call("curl", *self.request(), expected=90)

    def test_missing_independent_token_does_not_accept_negative(self):
        self.env.update(INPUT_MODE="rejected", EXPECTED_DEFAULT_TOKEN="")
        self.assertNotEqual(self.call("curl", *self.request(None)).returncode, 0)
        self.assertNotEqual(self.call("verify", "rejected", "failure").returncode, 0)

    def test_split_header_argument_is_rejected(self):
        args = self.request()
        args[2:3] = args[2].split(" ")
        self.call("curl", *args, expected=90)

    def test_unknown_binary_command_is_rejected(self):
        self.call("wfctl", "update", expected=91)

    def test_missing_binary_execution_fails_success_oracle(self):
        self.call("curl", *self.request(), expected=0)
        self.assertNotEqual(self.call("verify", "default", "success").returncode, 0)

    def test_negative_cannot_execute_binary(self):
        self.env["INPUT_MODE"] = "rejected"
        self.call("curl", *self.request(), expected=22)
        self.call("wfctl", "version", expected=0)
        self.assertNotEqual(self.call("verify", "rejected", "failure").returncode, 0)

    def test_action_failure_alone_is_not_negative_proof(self):
        self.assertNotEqual(self.call("verify", "rejected", "failure").returncode, 0)

    def test_wrong_outcome_fails_oracle(self):
        self.successful_calls()
        self.assertNotEqual(self.call("verify", "default", "failure").returncode, 0)

    def test_changed_path_fails_oracle(self):
        self.successful_calls()
        self.env["PATH"] = "/usr/bin:/bin"
        self.assertNotEqual(self.call("verify", "default", "success").returncode, 0)

    def test_unexpected_installed_binary_fails_oracle(self):
        self.successful_calls()
        installed = self.owned / "install" / "wfctl"
        installed.parent.mkdir()
        installed.write_text("unexpected")
        self.assertNotEqual(self.call("verify", "default", "success").returncode, 0)

    def test_extra_receipt_payload_is_rejected(self):
        self.successful_calls()
        (self.owned / "default.jsonl").write_text('{"event":"latest_response","extra":"unexpected"}\n')
        self.assertNotEqual(self.call("verify", "default", "success").returncode, 0)

    def test_malformed_receipt_is_rejected_without_echo(self):
        (self.owned / "default.jsonl").write_text("malformed-private-payload\n")
        result = self.call("verify", "default", "success")
        self.assertNotEqual(result.returncode, 0)
        self.assertNotIn("malformed-private-payload", result.stdout + result.stderr)

    def test_preexisting_cache_is_preserved_and_rejected(self):
        cache = Path(self.env["RUNNER_TOOL_CACHE"]) / "setup-wfctl"
        cache.mkdir(parents=True)
        (cache / "keep").write_text("preexisting")
        self.assertNotEqual(self.call("init").returncode, 0)
        self.assertEqual((cache / "keep").read_text(), "preexisting")

    def test_unowned_cleanup_is_rejected(self):
        (self.owned / ".owned").unlink()
        self.assertNotEqual(self.call("cleanup").returncode, 0)
        self.assertTrue(self.owned.is_dir())

    def test_owned_cleanup_removes_only_fixture_root(self):
        keep = self.root / "unrelated"
        keep.write_text("keep")
        self.call("cleanup", expected=0)
        self.assertFalse(self.owned.exists())
        self.assertEqual(keep.read_text(), "keep")


if __name__ == "__main__":
    unittest.main()
