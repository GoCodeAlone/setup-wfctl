"""Offline contract tests of the actual action's version-consuming shell steps.

Only disposable shell fixtures execute. No release, network or cache service is
used. Synthetic payloads reproduce the released CLI's stderr version contract.
"""

import os
from pathlib import Path
import subprocess
import tempfile
import textwrap
import unittest

ACTION = Path(os.environ.get("SETUP_WFCTL_ACTION", Path(__file__).parents[1] / "action.yml")).resolve()
TARGET = "v0.87.0"
WARNING = "WARNING: sonic/ast only supports (go1.17~1.26 and amd64 CPU) or (go1.20~1.26 and arm64 CPU), but your environment is not suitable and will fallback to encoding/json"


def script_named(name):
    marker = "    - name: " + name + "\n"
    source = ACTION.read_text()
    if source.count(marker) != 1:
        raise AssertionError("expected exactly one action step " + name)
    block = source.split(marker, 1)[1].split("    - name: ", 1)[0]
    return textwrap.dedent(block.split("      run: |\n", 1)[1])


def emit(data, stream):
    encoded = "".join("\\%03o" % byte for byte in data)
    return "printf '%b' '" + encoded + "' >&" + str(stream) + "\n"


class VersionStreams(unittest.TestCase):
    def setUp(self):
        tmp = tempfile.TemporaryDirectory(prefix="version stream ")
        self.addCleanup(tmp.cleanup)
        self.root = Path(tmp.name)
        self.bin = self.root / "bin"
        self.bin.mkdir()
        self.scratch = self.root / "scratch"
        self.scratch.mkdir()
        self.output = self.root / "github-output"
        self.events = self.root / "events"

    def payload(self, stdout, stderr, status=0):
        self.executable("#!/bin/sh\n[ \"$1\" = version ] || exit 99\n"
                        "printf '%s\\n' version >> \"$EVENTS\"\n"
                        + emit(stdout, 1) + emit(stderr, 2) + "exit " + str(status) + "\n")

    def executable(self, source):
        path = self.bin / "wfctl"
        path.write_text(source)
        path.chmod(0o755)

    def run_step(self, route, *, skip_update=True, use_existing=False, install_dir=None):
        self.output.unlink(missing_ok=True)
        self.events.unlink(missing_ok=True)
        env = {"PATH": str(self.bin) + ":/usr/bin:/bin", "HOME": str(self.root),
               "TMPDIR": str(self.scratch), "RUNNER_TEMP": str(self.scratch),
               "GITHUB_ACTION_PATH": str(ACTION.parent), "GITHUB_OUTPUT": str(self.output),
               "TARGET_VERSION": TARGET, "UPDATE_TIMEOUT_SECONDS": "1",
               "SKIP_UPDATE": "true" if skip_update else "false",
               "REQUESTED_INSTALL_DIR": str(self.bin), "RESOLVED_INSTALL_DIR": str(self.bin),
               "INSTALL_DIR": str(install_dir or self.bin),
               "USE_EXISTING": "true" if use_existing else "false", "EVENTS": str(self.events),
               "STATE": str(self.root / "updated"), "LC_ALL": "C"}
        name = "Verify installation" if route == "final" else "Reuse or update existing wfctl"
        result = subprocess.run(["/bin/bash", "--noprofile", "--norc", "-e", "-o", "pipefail", "-c", script_named(name)],
                                cwd=self.root, env=env, capture_output=True, timeout=5)
        self.assertEqual(list(self.scratch.iterdir()), [], "version scratch leaked")
        accepted = result.returncode == 0 if route == "final" else (
            result.returncode == 0 and self.output.exists() and "use_existing=true\n" in self.output.read_text())
        return accepted, result

    def test_version_contract_both_consumers(self):
        cases = [
            ("stdout", b"v0.87.0\n", b"", 0, True),
            ("stderr", b"", b"v0.87.0\n", 0, True),
            ("stdout_no_final_lf", b"v0.87.0", b"", 0, True),
            ("stderr_no_final_lf", b"", b"v0.87.0", 0, True),
            ("stderr_known_warning", b"", (WARNING + "\nv0.87.0\n").encode(), 0, True),
            ("stderr_warning_after", b"", ("v0.87.0\n" + WARNING + "\n").encode(), 0, True),
            ("stdout_known_stderr_warning", b"v0.87.0\n", (WARNING + "\n").encode(), 0, True),
            ("wrong_stdout", b"v0.86.0\n", b"", 0, False),
            ("wrong_stderr", b"", b"v0.86.0\n", 0, False),
            ("noise_only", b"", b"WARNING: v0.87.0 unavailable\n", 0, False),
            ("known_warning_only", b"", (WARNING + "\n").encode(), 0, False),
            ("arbitrary_stderr", b"v0.87.0\n", b"WARNING: arbitrary text\n", 0, False),
            ("arbitrary_stdout", b"ready\n", b"v0.87.0\n", 0, False),
            ("mixed_stream_versions", b"v0.87.0\n", b"v0.86.0\n", 0, False),
            ("mixed_same_stream", b"", b"v0.86.0\nv0.87.0\n", 0, False),
            ("duplicate_stream_versions", b"v0.87.0\n", b"v0.87.0\n", 0, False),
            ("duplicate_same_stream", b"v0.87.0\nv0.87.0\n", b"", 0, False),
            ("nonzero_stdout", b"v0.87.0\n", b"", 7, False),
            ("nonzero_stderr", b"", b"v0.87.0\n", 7, False),
            ("nonzero_known_warning", b"", (WARNING + "\nv0.87.0\n").encode(), 7, False),
            ("split_whitespace", b"v0.\n87.0\n", b"", 0, False),
            ("extra_blank", b"v0.87.0\n\n", b"", 0, False),
            ("leading_space", b" v0.87.0\n", b"", 0, False),
            ("embedded_nul", b"v0.87.0\0\n", b"", 0, False),
            ("carriage_return", b"v0.87.0\r\n", b"", 0, False),
            ("known_warning_stdout", (WARNING + "\nv0.87.0\n").encode(), b"", 0, False),
            ("duplicate_warning", b"v0.87.0\n", (WARNING + "\n" + WARNING + "\n").encode(), 0, False),
            ("empty", b"", b"", 0, False),
        ]
        for route in ("final", "probe"):
            for name, stdout, stderr, status, expected in cases:
                with self.subTest(route=route, case=name):
                    self.payload(stdout, stderr, status)
                    accepted, result = self.run_step(route)
                    self.assertEqual(accepted, expected, result.stderr.decode(errors="replace"))
                    self.assertEqual(self.events.read_text().splitlines(), ["version"])
                    if expected and WARNING.encode() in stderr:
                        self.assertIn(WARNING.encode(), result.stderr, "diagnostic was discarded")

    def test_existing_path_final_verification(self):
        self.payload(b"", b"v0.87.0\n")
        accepted, result = self.run_step("final", use_existing=True)
        self.assertTrue(accepted, result.stderr)

    def test_installation_checks_absolute_target_not_other_path_binary(self):
        self.payload(b"v0.87.0\n", b"")
        other = self.root / "other installation"
        other.mkdir()
        wrong = other / "wfctl"
        wrong.write_text("#!/bin/sh\nprintf '%s\\n' v0.86.0 >&2\n")
        wrong.chmod(0o755)
        accepted, _ = self.run_step("final", install_dir=other)
        self.assertFalse(accepted)
        self.assertFalse(self.events.exists(), "unrelated PATH binary was executed")

    def test_updated_binary_must_pass_same_contract(self):
        for status in (0, 7):
            with self.subTest(status=status):
                (self.root / "updated").unlink(missing_ok=True)
                self.executable("#!/bin/sh\nprintf '%s\\n' \"$1\" >> \"$EVENTS\"\n"
                                "if [ \"$1\" = update ]; then : > \"$STATE\"; exit 0; fi\n"
                                "[ \"$1\" = version ] || exit 99\n"
                                "if [ -e \"$STATE\" ]; then printf '%s\\n' v0.87.0 >&2; exit " + str(status) + "; fi\n"
                                "printf '%s\\n' v0.86.0 >&2\n")
                accepted, result = self.run_step("probe", skip_update=False)
                self.assertEqual(accepted, status == 0, result.stderr)
                self.assertEqual(self.events.read_text().splitlines(), ["version", "update", "version"])

    def helper_call(self, expected=TARGET, *, binary=None, suffix=""):
        env = {"PATH": "/usr/bin:/bin", "HOME": str(self.root), "RUNNER_TEMP": str(self.scratch),
               "EVENTS": str(self.events), "BIN": str(binary or self.bin / "wfctl"),
               "EXPECTED": expected, "HELPER": str(ACTION.parent / "scripts/read-version.sh"),
               "MARKER": str(self.root / "outer-exit"), "LC_ALL": "C"}
        result = subprocess.run(["/bin/bash", "--noprofile", "--norc", "-c",
                                 'source "$HELPER"; ' + suffix + 'wfctl_read_version "$BIN" "$EXPECTED"'],
                                cwd=self.root, env=env, capture_output=True, timeout=5)
        self.assertEqual(list(self.scratch.iterdir()), [])
        return result

    def test_helper_preserves_nonzero_exit_and_never_returns_version(self):
        self.payload(b"v0.87.0\n", b"", 7)
        result = self.helper_call()
        self.assertEqual(result.returncode, 7)
        self.assertEqual(result.stdout, b"")

    def test_helper_rejects_invalid_expectation_and_relative_binary(self):
        self.payload(b"v0.87.0\n", b"")
        for expected in ("", "v0.87.0\n", "v0.87.0\rv0.86.0"):
            with self.subTest(expected=expected):
                self.assertEqual(self.helper_call(expected).returncode, 2)
        self.assertEqual(self.helper_call(binary="bin/wfctl").returncode, 2)
        self.assertFalse(self.events.exists())

    def test_helper_does_not_replace_caller_exit_trap(self):
        self.payload(b"", b"v0.87.0\n")
        result = self.helper_call(suffix='trap \'printf outer > "$MARKER"\' EXIT; ')
        self.assertEqual(result.returncode, 0, result.stderr)
        self.assertEqual((self.root / "outer-exit").read_text(), "outer")


if __name__ == "__main__":
    unittest.main(verbosity=2)
