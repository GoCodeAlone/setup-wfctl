"""Synthetic release boundary for the real hosted composite-action input check.

This never contacts GitHub or records credentials. The hosted workflow, not this
helper, evaluates action defaults and runs the pinned action's actual steps.
"""

import json
import os
from pathlib import Path
import re
import shlex
import shutil
import sys
import tempfile


MODES = ("default", "empty", "rejected")
MARKER = "setup-wfctl hosted input fixture v1\n"
URL = "https://api.github.com/repos/GoCodeAlone/workflow/releases/latest"
ACCEPT = "Accept: application/vnd.github+json"
EVENTS = {"latest_response", "wfctl_version", "rejected_default_header",
          "unexpected_request", "unexpected_binary_call"}


def require(condition, message):
    if not condition:
        raise SystemExit(message)


def no_cache():
    cache = Path(os.environ.get("RUNNER_TOOL_CACHE") or os.environ["RUNNER_TEMP"]) / "setup-wfctl"
    require(not cache.exists() and not cache.is_symlink(), "Unexpected cache root; left untouched")


def owned_root():
    root = Path(os.environ["HOSTED_INPUT_ROOT"])
    runner_temp = Path(os.environ["RUNNER_TEMP"]).resolve()
    require(root.is_absolute() and not root.is_symlink()
            and root.parent.resolve() == runner_temp
            and root.name.startswith("setup-wfctl-input-"), "Unowned fixture root")
    marker = root / ".owned"
    require(marker.is_file() and not marker.is_symlink()
            and marker.read_text() == MARKER, "Missing fixture ownership marker")
    return root


def mode_name(value):
    require(value in MODES, "Unknown fixture case")
    return value


def event(name):
    require(name in EVENTS, "Unknown fixture event")
    mode = mode_name(os.environ["INPUT_MODE"])
    with (owned_root() / (mode + ".jsonl")).open("a") as output:
        output.write(json.dumps({"event": name}) + "\n")


def init():
    no_cache()
    run_id, attempt = os.environ["GITHUB_RUN_ID"], os.environ["GITHUB_RUN_ATTEMPT"]
    require(re.fullmatch(r"[0-9]+", run_id) and re.fullmatch(r"[0-9]+", attempt),
            "Invalid run identity")
    version = "v0.0.0-hosted-input-" + run_id + "-" + attempt
    root = Path(tempfile.mkdtemp(prefix="setup-wfctl-input-", dir=os.environ["RUNNER_TEMP"])).resolve()
    try:
        require("\n" not in str(root) and "\r" not in str(root), "Invalid fixture path")
        (root / ".owned").write_text(MARKER)
        (root / "bin").mkdir()
        command = shlex.quote(sys.executable) + " -I -B " + shlex.quote(str(Path(__file__).resolve()))
        for name in ("curl", "wfctl"):
            wrapper = root / "bin" / name
            wrapper.write_text("#!/bin/sh\nexec " + command + " " + name + ' "$@"\n')
            wrapper.chmod(0o755)
        with Path(os.environ["GITHUB_ENV"]).open("a") as output:
            output.write("HOSTED_INPUT_ROOT=" + str(root) + "\nHOSTED_INPUT_VERSION=" + version + "\n")
        with Path(os.environ["GITHUB_PATH"]).open("a") as output:
            output.write(str(root / "bin") + "\n")
    except BaseException:
        shutil.rmtree(root)
        raise
    print("Owned hosted input fixture prepared")


def curl(args):
    mode = mode_name(os.environ["INPUT_MODE"])
    expected = ["-fsSL"]
    token = os.environ.get("EXPECTED_DEFAULT_TOKEN", "")
    if mode != "empty":
        require(bool(token), "Independent default-token expectation is missing")
        expected += ["-H", "Authorization: Bearer " + token]
    expected += ["-H", ACCEPT, URL]
    if args != expected:
        event("unexpected_request")
        return 90
    if mode == "rejected":
        event("rejected_default_header")
        return 22
    event("latest_response")
    print(json.dumps({"tag_name": os.environ["HOSTED_INPUT_VERSION"]}))
    return 0


def wfctl(args):
    if args != ["version"]:
        event("unexpected_binary_call")
        return 91
    event("wfctl_version")
    print(os.environ["HOSTED_INPUT_VERSION"])
    return 0


def receipt(root, mode):
    path = root / (mode + ".jsonl")
    require(path.is_file() and not path.is_symlink(), "Missing fixture receipt")
    try:
        rows = [json.loads(line) for line in path.read_text().splitlines()]
    except json.JSONDecodeError:
        raise SystemExit("Invalid fixture receipt") from None
    require(all(isinstance(row, dict) and set(row) == {"event"}
                and row["event"] in EVENTS for row in rows), "Invalid fixture receipt")
    return [row["event"] for row in rows]


def verify(mode, outcome):
    mode_name(mode)
    root = owned_root()
    no_cache()
    for name in ("curl", "wfctl"):
        require(shutil.which(name) == str(root / "bin" / name), "Fixture is not first on PATH")
    installed = root / "install" / "wfctl"
    require(not installed.exists() and not installed.is_symlink(), "Unexpected installation")
    expected = (["rejected_default_header"] if mode == "rejected"
                else ["latest_response", "wfctl_version", "wfctl_version"])
    require(outcome == ("failure" if mode == "rejected" else "success"), "Unexpected action outcome")
    events = receipt(root, mode)
    require(events == expected, "Unexpected action execution sequence")
    print(json.dumps({"mode": mode, "outcome": outcome, "events": events}))


def cleanup():
    root = owned_root()
    # A missing/failed case is diagnosed by verify; cleanup still removes owned data.
    shutil.rmtree(root)
    require(not root.exists() and not root.is_symlink(), "Fixture cleanup incomplete")
    print("Owned hosted input fixture removed; absence verified")


def main(args):
    if args == ["init"]:
        init()
    elif args and args[0] == "curl":
        return curl(args[1:])
    elif args and args[0] == "wfctl":
        return wfctl(args[1:])
    elif len(args) == 3 and args[0] == "verify":
        verify(args[1], args[2])
    elif args == ["cleanup"]:
        cleanup()
    else:
        raise SystemExit("Invalid fixture command")
    return 0


if __name__ == "__main__":
    sys.exit(main(sys.argv[1:]))
