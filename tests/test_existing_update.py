"""Exercise the existing-binary step offline with an isolated command PATH.

Only synthetic wfctl binaries run. No installation, release request, or real
runner binary is used. Set SETUP_WFCTL_ACTION to test an alternate action file
or SETUP_WFCTL_BASH to test an installed Bash version.
"""

import json
import os
from pathlib import Path
import shutil
import subprocess
import sys
import tempfile
import textwrap
import unittest


ACTION = Path(os.environ.get("SETUP_WFCTL_ACTION", Path(__file__).parents[1] / "action.yml"))
BASH = os.environ.get("SETUP_WFCTL_BASH", "/bin/bash")
TIMEOUT = shutil.which("timeout")


def existing_script():
    source = ACTION.read_text()
    step = source.split("    - name: Reuse or update existing wfctl\n", 1)[1]
    step = step.split("    - name: ", 1)[0]
    script = textwrap.dedent(step.split("      run: |\n", 1)[1])
    assert "${{" not in script
    return script


FAKE_WFCTL = """#!{python}
import json, os, pathlib, sys, time
root = pathlib.Path(os.environ["TEST_CASE_ROOT"])
args = sys.argv[1:]
with (root / "events.jsonl").open("a") as stream:
    stream.write(json.dumps({{"args": args, "no_update_check": os.environ.get("WFCTL_NO_UPDATE_CHECK")}}) + "\\n")
if args == ["version"]:
    version = root / "version"
    print(version.read_text() if version.exists() else os.environ["TEST_CURRENT_VERSION"])
elif args == ["update"]:
    time.sleep(float(os.environ["TEST_UPDATE_DELAY"]))
    if os.environ["TEST_UPDATE_EXIT"] != "0":
        sys.exit(int(os.environ["TEST_UPDATE_EXIT"]))
    (root / "version").write_text(os.environ["TEST_UPDATED_VERSION"])
    (root / "update_completed").touch()
else:
    sys.exit(99)
"""


class ExistingUpdateTests(unittest.TestCase):
    def setUp(self):
        temporary = tempfile.TemporaryDirectory(prefix="setup-wfctl-existing-")
        self.addCleanup(temporary.cleanup)
        self.root = Path(temporary.name)
        self.bin_dir = self.root / "bin"
        self.bin_dir.mkdir()
        # Never inherit a host wfctl or timeout through PATH.
        for command in ("tr", "env", "dirname", "mkdir"):
            installed = shutil.which(command)
            self.assertIsNotNone(installed, command)
            (self.bin_dir / command).symlink_to(installed)
        self.wfctl = self.bin_dir / "wfctl"
        self.wfctl.write_text(FAKE_WFCTL.format(python=sys.executable))
        self.wfctl.chmod(0o755)
        self.env = {
            "PATH": str(self.bin_dir),
            "HOME": str(self.root),
            "LANG": "C",
            "PYTHONDONTWRITEBYTECODE": "1",
            "TARGET_VERSION": "v-new",
            "UPDATE_TIMEOUT_SECONDS": "1",
            "SKIP_UPDATE": "false",
            "REQUESTED_INSTALL_DIR": "",
            "RESOLVED_INSTALL_DIR": str(self.root / "install"),
            "GITHUB_OUTPUT": str(self.root / "github-output"),
            "TEST_CASE_ROOT": str(self.root),
            "TEST_CURRENT_VERSION": "v-old",
            "TEST_UPDATED_VERSION": "v-new",
            "TEST_UPDATE_DELAY": "0",
            "TEST_UPDATE_EXIT": "0",
        }

    def with_timeout(self):
        if not TIMEOUT:
            self.skipTest("The real timeout command is not installed")
        (self.bin_dir / "timeout").symlink_to(TIMEOUT)

    def run_step(self, expected):
        result = subprocess.run(
            [BASH, "--noprofile", "--norc", "-c", existing_script()],
            cwd=self.root, env=self.env, capture_output=True, text=True,
            # The longest synthetic update finishes in 2.2 seconds, even if
            # the action regresses. No hanging or child-spawning fixture runs.
            timeout=6,
        )
        self.assertEqual(result.returncode, 0, result.stderr)
        output = Path(self.env["GITHUB_OUTPUT"]).read_text().splitlines()
        self.assertEqual(output, ["use_existing=" + expected], result.stdout)
        path = self.root / "events.jsonl"
        self.events = [json.loads(line) for line in path.read_text().splitlines()] if path.exists() else []
        self.assertTrue(all(event["no_update_check"] == "1" for event in self.events))
        return result

    def assert_no_update(self):
        self.assertNotIn(["update"], [event["args"] for event in self.events])
        self.assertFalse((self.root / "update_completed").exists())

    def test_missing_timeout_skips_slow_update(self):
        self.env["TEST_UPDATE_DELAY"] = "2.2"
        result = self.run_step("false")
        self.assert_no_update()
        self.assertIn("No timeout command available", result.stdout)

    def test_missing_timeout_skips_even_fast_update(self):
        self.run_step("false")
        self.assert_no_update()

    def test_matching_version_reused_without_timeout(self):
        self.env["TEST_CURRENT_VERSION"] = "v-new"
        self.run_step("true")
        self.assert_no_update()

    def test_skip_update_without_timeout(self):
        self.env["SKIP_UPDATE"] = "true"
        self.run_step("false")
        self.assert_no_update()

    def test_skip_update_with_timeout(self):
        self.with_timeout()
        self.env["SKIP_UPDATE"] = "true"
        self.run_step("false")
        self.assert_no_update()

    def test_no_existing_binary_uses_download(self):
        self.wfctl.unlink()
        self.run_step("false")
        self.assertEqual(self.events, [])

    def test_unrelated_install_directory_skips_update(self):
        self.with_timeout()
        self.env["REQUESTED_INSTALL_DIR"] = self.env["RESOLVED_INSTALL_DIR"]
        self.run_step("false")
        self.assert_no_update()

    def test_matching_install_directory_still_needs_timeout(self):
        self.env["REQUESTED_INSTALL_DIR"] = str(self.bin_dir)
        self.env["RESOLVED_INSTALL_DIR"] = str(self.bin_dir)
        self.run_step("false")
        self.assert_no_update()

    def test_successful_bounded_update_is_reused(self):
        self.with_timeout()
        self.run_step("true")
        self.assertEqual([event["args"] for event in self.events], [["version"], ["update"], ["version"]])
        self.assertTrue((self.root / "update_completed").exists())

    def test_bounded_update_with_wrong_version_uses_download(self):
        self.with_timeout()
        self.env["TEST_UPDATED_VERSION"] = "v-other"
        self.run_step("false")
        self.assertTrue((self.root / "update_completed").exists())

    def test_failed_update_uses_download(self):
        self.with_timeout()
        self.env["TEST_UPDATE_EXIT"] = "23"
        self.run_step("false")
        self.assertEqual([event["args"] for event in self.events], [["version"], ["update"]])
        self.assertFalse((self.root / "update_completed").exists())

    def test_real_timeout_interrupts_slow_update(self):
        self.with_timeout()
        self.env["TEST_UPDATE_DELAY"] = "2.2"
        self.run_step("false")
        self.assertEqual([event["args"] for event in self.events], [["version"], ["update"]])
        self.assertFalse((self.root / "update_completed").exists())

    def test_timeout_rejecting_invalid_duration_uses_download(self):
        self.with_timeout()
        self.env["UPDATE_TIMEOUT_SECONDS"] = "not-a-duration"
        self.run_step("false")
        self.assert_no_update()


if __name__ == "__main__":
    unittest.main()
