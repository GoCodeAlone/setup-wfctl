"""Exercise installation verification with fake binaries, never a real runner."""

import os
from pathlib import Path
import re
import subprocess
import tempfile
import textwrap
import unittest


ACTION = Path(os.environ.get("SETUP_WFCTL_ACTION", Path(__file__).parents[1] / "action.yml"))


def step_script(name):
    source = ACTION.read_text()
    step = source.split(f"    - name: {name}\n", 1)[1].split("    - name: ", 1)[0]
    scalar = re.search(r"^      run: (.+)$", step, re.M)
    if scalar.group(1) != "|":
        return scalar.group(1)
    return textwrap.dedent(step[scalar.end() + 1 :])


class InstallationTests(unittest.TestCase):
    def setUp(self):
        self.tmp = tempfile.TemporaryDirectory()
        self.addCleanup(self.tmp.cleanup)
        self.root = Path(self.tmp.name)
        self.installed = self.root / "installed"
        self.shadow = self.root / "shadow"
        self.installed.mkdir()
        self.shadow.mkdir()
        self.bin(self.installed, "v0.60.6")
        self.bin(self.shadow, "v0.83.0")
        self.env = {
            **os.environ,
            "PATH": str(self.shadow) + os.pathsep + os.environ["PATH"],
            "TARGET_VERSION": "v0.60.6",
            "INSTALL_DIR": str(self.installed),
            "USE_EXISTING": "false",
            "GITHUB_PATH": str(self.root / "github-path"),
            "GITHUB_OUTPUT": str(self.root / "github-output"),
        }

    def bin(self, directory, version):
        binary = directory / "wfctl"
        binary.write_text(f"#!/bin/sh\n[ \"$1\" = version ] || exit 99\nprintf '%s\\n' '{version}'\n")
        binary.chmod(0o755)

    def run_step(self, name, install_dir=None):
        script = step_script(name).replace(
            "${{ steps.install.outputs.install_dir }}", str(self.installed)
        ).replace(
            "${{ inputs.install-dir }}", install_dir if install_dir is not None else str(self.installed)
        )
        return subprocess.run(
            ["/bin/bash", "-c", script], env=self.env,
            cwd=self.root, capture_output=True, text=True, timeout=5,
        )

    def test_installed_binary_wins_over_shadow_in_verification(self):
        result = self.run_step("Verify installation")
        self.assertEqual(result.returncode, 0, result.stderr)
        self.assertIn("Verified wfctl v0.60.6", result.stdout)
        self.assertIn(str(self.installed / "wfctl"), result.stdout)

    def test_wrong_cached_binary_fails_even_with_matching_path_binary(self):
        self.bin(self.installed, "v0.83.0")
        self.bin(self.shadow, "v0.60.6")
        result = self.run_step("Verify installation")
        self.assertNotEqual(result.returncode, 0)
        self.assertIn("Expected wfctl v0.60.6", result.stderr)

    def test_missing_installed_binary_does_not_use_path_fallback(self):
        (self.installed / "wfctl").unlink()
        result = self.run_step("Verify installation")
        self.assertNotEqual(result.returncode, 0)

    def test_reused_matching_binary_is_verified(self):
        self.env["USE_EXISTING"] = "true"
        self.env["TARGET_VERSION"] = "v0.83.0"
        result = self.run_step("Verify installation")
        self.assertEqual(result.returncode, 0, result.stderr)
        self.assertIn(str(self.shadow / "wfctl"), result.stdout)

    def test_reused_changed_binary_fails(self):
        self.env["USE_EXISTING"] = "true"
        result = self.run_step("Verify installation")
        self.assertNotEqual(result.returncode, 0)

    def test_default_install_directory_gets_path_precedence(self):
        source = ACTION.read_text().split("    - name: Add install directory to PATH\n", 1)[1]
        condition = source.splitlines()[0].strip()
        self.assertEqual(condition, "if: steps.existing.outputs.use_existing != 'true'")
        result = self.run_step("Add install directory to PATH")
        self.assertEqual(result.returncode, 0, result.stderr)
        installed_path = Path(self.env["GITHUB_PATH"]).read_text().strip()
        self.env["PATH"] = installed_path + os.pathsep + self.env["PATH"]
        result = subprocess.run(
            ["/bin/bash", "-c", "wfctl version"], env=self.env,
            capture_output=True, text=True, timeout=5,
        )
        self.assertEqual(result.returncode, 0, result.stderr)
        self.assertEqual(result.stdout.strip(), "v0.60.6")

    def test_relative_install_directory_survives_later_working_directory(self):
        result = self.run_step("Resolve install directory", install_dir="tools/wfctl-bin")
        self.assertEqual(result.returncode, 0, result.stderr)
        outputs = dict(line.split("=", 1) for line in Path(self.env["GITHUB_OUTPUT"]).read_text().splitlines())
        self.installed = Path(outputs["install_dir"])
        self.assertTrue(self.installed.is_absolute())
        self.bin(self.installed, "v0.60.6")
        self.env["INSTALL_DIR"] = str(self.installed)
        result = self.run_step("Verify installation")
        self.assertEqual(result.returncode, 0, result.stderr)
        result = self.run_step("Add install directory to PATH")
        self.assertEqual(result.returncode, 0, result.stderr)
        self.env["PATH"] = Path(self.env["GITHUB_PATH"]).read_text().strip() + os.pathsep + self.env["PATH"]
        other = self.root / "other-working-directory"
        other.mkdir()
        result = subprocess.run(
            ["/bin/bash", "-c", "wfctl version"], cwd=other, env=self.env,
            capture_output=True, text=True, timeout=5,
        )
        self.assertEqual(result.returncode, 0, result.stderr)
        self.assertEqual(result.stdout.strip(), "v0.60.6")


if __name__ == "__main__":
    unittest.main()
