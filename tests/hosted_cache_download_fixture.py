"""Inert hosted-fixture source draft. Local tests inject fake transfer executors.

The CLI's real-release mode is for a separately authorized future hosted run.
It is never exercised by local tests. Cache evidence is a derived branch witness,
not a direct cache-service receipt, and does not invent checksum diagnostics.
"""
import hashlib
import json
import os
from pathlib import Path
import platform
import re
import shlex
import shutil
import stat
import subprocess
import sys
import tempfile

ACTION = "6b37f3522cc7a1ab88849c87b208219bd49e3cc4"
CURL_ARGS = ["--fail", "--show-error", "--silent", "--location", "--connect-timeout", "30", "--max-time", "120"]
RELEASE_SPEC = {
    "binary": {"size":146747936, "sha256":"50e35f55d433f570b5f0e45274e8f505127c433c7e828917eb101a8e49995fc1"},
    "manifest": {"size":1962, "sha256":"0495c9740d4204d79ceee9be9e932278ea9237881258a40acfa621d0cf56d91e"},
}
CASES = ("valid-hit", "tampered-hit", "miss", "real-release")
MARKER = "setup-wfctl hosted cache/download source draft v1\n"


def require(condition, message):
    if not condition:
        raise ValueError(message)


def file_info(path):
    info = Path(path).lstat()
    require(stat.S_ISREG(info.st_mode), "expected nonsymlink regular file")
    sha = hashlib.sha256()
    with Path(path).open("rb") as source:
        for block in iter(lambda: source.read(65536), b""):
            sha.update(block)
    return {"size":info.st_size, "sha256":sha.hexdigest()}


def save(root, state):
    pending = root / "state.next"
    with pending.open("x") as output:
        json.dump(state, output, indent=2)
        output.write("\n")
    pending.replace(root / "state.json")


def load(root):
    root = Path(root)
    require(not root.is_symlink() and root.is_dir(), "unowned root")
    require((root / ".owned").is_file() and not (root / ".owned").is_symlink()
            and (root / ".owned").read_text() == MARKER, "missing ownership marker")
    require(not (root / "state.json").is_symlink(), "invalid state file")
    state = json.loads((root / "state.json").read_text())
    require(state["root"] == str(root.resolve()) and state["action"] == ACTION, "state identity mismatch")
    require(state["cache_dir"] == str(Path(state["cache_root"]) / "setup-wfctl" / state["version"] / "linux-amd64"), "cache path mismatch")
    return state


def prepare(root, runner_temp, legacy_dir, case, run_id, attempt, real_curl, cache_root):
    root, runner_temp, legacy_dir = Path(root), Path(runner_temp), Path(legacy_dir)
    require(case in CASES and re.fullmatch(r"[0-9]+", run_id) and re.fullmatch(r"[0-9]+", attempt), "invalid case/run identity")
    require(not root.is_symlink() and root.is_dir() and not list(root.iterdir()), "root must be empty")
    require(root.parent.resolve() == runner_temp.resolve(), "root outside runner temp")
    require(not legacy_dir.is_symlink() and legacy_dir.is_dir(), "invalid legacy temp directory")
    cache_root = Path(cache_root)
    require(cache_root.is_absolute() and not cache_root.is_symlink() and cache_root.is_dir(), "invalid actual runner cache root")
    parent = cache_root / "setup-wfctl"
    require(not parent.is_symlink() and (not parent.exists() or parent.is_dir()), "invalid cache parent")
    for name in ("wfctl", "wfctl-checksums.txt"):
        path = legacy_dir / name
        require(not path.exists() and not path.is_symlink(), "pre-existing legacy path; left untouched")
    for path in (root, runner_temp, legacy_dir, Path(real_curl)):
        require("\n" not in str(path) and "\r" not in str(path), "unsafe path")
    root, runner_temp, legacy_dir = root.resolve(), runner_temp.resolve(), legacy_dir.resolve()
    invocation = f"{run_id}-{attempt}-{case}"
    version = "v0.87.0" if case == "real-release" else "v0.0.0-hosted-cache-" + invocation
    version_dir = parent / version
    require(not version_dir.exists() and not version_dir.is_symlink(), "selected cache version path occupied; preserved")
    version_dir.mkdir(parents=True, exist_ok=False)
    cache = version_dir / "linux-amd64"
    for directory in (cache, root / "pristine", root / "install", root / "bin", root / "curl-home"):
        directory.mkdir(parents=True)
    event = json.dumps({"invocation":invocation, "kind":"execute"}, separators=(",", ":"))
    payload = ("#!/bin/sh\n[ \"$#\" -eq 1 ] && [ \"$1\" = version ] || exit 99\n"
               + "printf '%s\\n' " + shlex.quote(event) + " >> " + shlex.quote(str(root / "events.jsonl")) + "\n"
               + "printf '%s\\n' " + shlex.quote(version) + "\n").encode()
    (root / "pristine/binary").write_bytes(payload)
    manifest = hashlib.sha256(payload).hexdigest() + "  wfctl-linux-amd64\n"
    (root / "pristine/checksums.txt").write_text(manifest)
    seed = payload + (b"# tampered\n" if case == "tampered-hit" else b"")
    if case in ("valid-hit", "tampered-hit"):
        (cache / "wfctl").write_bytes(seed)
        (cache / "wfctl").chmod(0o755)
    expected = (RELEASE_SPEC if case == "real-release" else
                {"binary":file_info(root / "pristine/binary"), "manifest":file_info(root / "pristine/checksums.txt")})
    state = {
        "root":str(root), "runner_temp":str(runner_temp), "legacy_dir":str(legacy_dir), "case":case,
        "action":ACTION, "invocation":invocation, "version":version, "phase":"prepared",
        "cache_root":str(cache_root), "cache_dir":str(cache), "cache_key":f"wfctl-{version}-linux-amd64",
        "url_base":f"https://github.com/GoCodeAlone/workflow/releases/download/{version}/",
        "seed_sha256":hashlib.sha256(seed).hexdigest(), "expected":expected,
        "real_curl":str(Path(real_curl).resolve()), "curl_info":file_info(Path(real_curl).resolve()),
        "owned_legacy":[], "seeded_local_binary_absent_before_action":False,
    }
    (root / ".owned").write_text(MARKER)
    save(root, state)
    return state


def arm(root):
    root = Path(root)
    state = load(root)
    require(state["phase"] == "prepared", "case must be prepared exactly once")
    cached = Path(state["cache_dir"]) / "wfctl"
    if state["case"] in ("valid-hit", "tampered-hit"):
        require(file_info(cached)["sha256"] == state["seed_sha256"], "seed changed after preparation")
        require(list(cached.parent.iterdir()) == [cached], "unexpected seeded cache contents")
        cached.unlink()  # only the exact owned seed, after the future native save step
    require(not cached.exists() and not cached.is_symlink(), "local cache binary unexpectedly present")
    require(not (root / "events.jsonl").exists(), "events present before invocation")
    state["phase"] = "armed"
    state["seeded_local_binary_absent_before_action"] = True
    save(root, state)


def events(root, state):
    path = Path(root) / "events.jsonl"
    if not path.exists():
        return []
    require(not path.is_symlink() and path.stat().st_size < 32768, "invalid events file")
    rows = [json.loads(line) for line in path.read_text().splitlines()]
    for row in rows:
        require(isinstance(row, dict) and row.get("invocation") == state["invocation"], "event invocation mismatch")
        expected = {"invocation", "kind"} if row.get("kind") == "execute" else {"invocation", "kind", "sha256", "size", "restored_sha256"}
        require(set(row) == expected, "event schema mismatch")
        if row["kind"] != "execute":
            require(row["kind"] in ("guard-manifest", "download-manifest", "download-binary"), "unknown event kind")
            kind = "binary" if row["kind"] == "download-binary" else "manifest"
            expected_value = state["expected"][kind]
            require(type(row["size"]) is int and row["size"] == expected_value["size"]
                    and isinstance(row["sha256"], str) and row["sha256"] == expected_value["sha256"], "event transfer digest/size mismatch")
            expected_restored = state["seed_sha256"] if row["kind"] == "guard-manifest" else None
            require(row["restored_sha256"] == expected_restored, "event restored digest mismatch")
    return rows


def fetch(root, args, executor):
    root = Path(root)
    state = load(root)
    require(state["phase"] == "armed", "case must be armed")
    require(len(args) == len(CURL_ARGS) + 3 and args[:len(CURL_ARGS)] == CURL_ARGS and args[-2] == "-o", "unexpected arguments")
    url, output = args[len(CURL_ARGS)], Path(args[-1])
    mapping = {state["url_base"] + "checksums.txt":("manifest", "checksums.txt"),
               state["url_base"] + "wfctl-linux-amd64":("binary", "binary")}
    require(url in mapping, "unexpected URL")
    kind, source = mapping[url]
    guard = (output.parent == Path(state["runner_temp"]) and re.fullmatch(r"setup-wfctl-checksums\.[A-Za-z0-9]{6}", output.name))
    prior = events(root, state)
    restored_sha = None
    if guard:
        require(state["case"] in ("valid-hit", "tampered-hit"), "unexpected cache-hit branch; download case unqualified")
        require(kind == "manifest" and not prior, "unexpected guard request order")
        require(output.is_file() and not output.is_symlink() and output.stat().st_size == 0, "invalid action-owned mktemp")
        restored_sha = file_info(Path(state["cache_dir"]) / "wfctl")["sha256"]
        require(restored_sha == state["seed_sha256"], "unexpected restored cache bytes")
        event_kind = "guard-manifest"
    else:
        require(state["case"] in ("miss", "real-release"), "seeded case entered download branch")
        require(output == Path(state["legacy_dir"]) / ("wfctl-checksums.txt" if kind == "manifest" else "wfctl"), "unexpected output path")
        required = [] if kind == "manifest" else ["download-manifest"]
        require([row["kind"] for row in prior] == required, "unexpected request order")
        # Reserve an absent fixed path before curl can create/truncate it. Record
        # identity before transfer so failure cleanup cannot remove someone else's file.
        with output.open("xb"):
            pass
        info = output.stat()
        state["owned_legacy"].append({"path":str(output), "device":info.st_dev, "inode":info.st_ino})
        save(root, state)
        event_kind = "download-" + kind
    original = output.lstat()
    expected = state["expected"][kind]
    if state["case"] == "real-release":
        require(executor is not None, "real transport executor required")
        require(file_info(state["real_curl"]) == state["curl_info"], "installed curl identity changed")
        home = str(root / "curl-home")
        env = {"PATH":"/usr/bin:/bin", "HOME":home, "CURL_HOME":home, "XDG_CONFIG_HOME":home, "LC_ALL":"C"}
        code = executor([state["real_curl"], *args], env=env, timeout=125)
        require(type(code) is int and code == 0, "transport failure")
    else:
        pristine = root / "pristine" / source
        require(file_info(pristine) == expected, "pristine fixture changed")
        output.write_bytes(pristine.read_bytes())
    after = output.lstat()
    require(stat.S_ISREG(after.st_mode) and (original.st_dev, original.st_ino) == (after.st_dev, after.st_ino), "output identity changed")
    require(after.st_size == expected["size"], "download size mismatch before action execution")
    require(file_info(output) == expected, "download size/digest mismatch before action execution")
    if kind == "manifest":
        selected = [line.split() for line in output.read_text().splitlines() if line.split() and line.split()[-1] == "wfctl-linux-amd64"]
        require(selected == [[state["expected"]["binary"]["sha256"], "wfctl-linux-amd64"]], "manifest/binary expectation mismatch")
    event = {"invocation":state["invocation"], "kind":event_kind, **expected, "restored_sha256":restored_sha}
    with (root / "events.jsonl").open("a") as log:
        log.write(json.dumps(event, sort_keys=True) + "\n")


def observe(root, outcome):
    root = Path(root)
    state = load(root)
    require(state["phase"] == "armed" and state["seeded_local_binary_absent_before_action"], "missing armed invocation")
    rows = events(root, state)
    case = state["case"]
    expected = (["guard-manifest"] if case in ("valid-hit", "tampered-hit") else ["download-manifest", "download-binary"])
    if case in ("valid-hit", "miss"):
        expected.append("execute")
    require([r["kind"] for r in rows] == expected, "missing or unexpected events")
    require(outcome == ("failure" if case == "tampered-hit" else "success"), "unexpected action outcome")
    installed = root / "install/wfctl"
    if case == "tampered-hit":
        require(not installed.exists() and not installed.is_symlink(), "tampered bytes installed")
    else:
        require(file_info(installed) == state["expected"]["binary"], "installed digest mismatch")
    receipt = {
        "action":ACTION, "invocation":state["invocation"], "case":case, "version":state["version"],
        "cache_key":state["cache_key"], "cache_dir":state["cache_dir"], "outcome":outcome,
        "seeded_local_binary_absent_before_action":True, "events":rows,
        "evidence_kind":"derived-native-hit-branch" if case in ("valid-hit", "tampered-hit") else "download-branch-observed",
        "healthy_cache_service_miss_proven":False, "checksum_diagnostic_verified":False,
        "qualification":"pending-actual-checksum-diagnostic" if case == "tampered-hit" else "observed-path-and-bytes-only",
        "cache_post_step":"not yet observed; owned cache remains for post-action save",
    }
    (root / "observation.json").write_text(json.dumps(receipt, indent=2) + "\n")
    state["phase"] = "observed"
    save(root, state)
    return receipt


def cleanup(root):
    root = Path(root)
    state = load(root)
    for claimed in state["owned_legacy"]:
        path = Path(claimed["path"])
        require(path.parent == Path(state["legacy_dir"]) and path.name in ("wfctl", "wfctl-checksums.txt"), "unowned cleanup path")
        if not path.exists() and not path.is_symlink():
            continue  # binary moved into the owned cache by the original action
        info = path.lstat()
        require(stat.S_ISREG(info.st_mode) and (info.st_dev, info.st_ino) == (claimed["device"], claimed["inode"]), "cleanup identity mismatch; preserved")
        path.unlink()
    # Do not delete the cache before actions/cache's post step. All remaining
    # owned paths live on the future disposable hosted runner until teardown.
    return {"legacy_cleanup":"complete", "owned_cache":"retained for native post step and runner teardown"}


def cli(args):
    if len(args) == 2 and args[0] == "init":
        require(platform.system() == "Linux" and platform.machine() in ("x86_64", "amd64"), "hosted draft requires Linux amd64")
        require(shutil.which("wfctl") is None, "pre-existing wfctl on PATH")
        curl = shutil.which("curl")
        require(curl is not None, "installed curl unavailable")
        runner = Path(os.environ["RUNNER_TEMP"]).resolve()
        root = Path(tempfile.mkdtemp(prefix="setup-wfctl-cache-", dir=runner))
        try:
            cache_root = Path(os.environ.get("RUNNER_TOOL_CACHE") or os.environ["RUNNER_TEMP"])
            state = prepare(root, runner, Path("/tmp"), args[1], os.environ["GITHUB_RUN_ID"], os.environ["GITHUB_RUN_ATTEMPT"], Path(curl), cache_root)
            command = shlex.quote(sys.executable) + " -I -B " + shlex.quote(str(Path(__file__).resolve()))
            wrapper = root / "bin/curl"
            wrapper.write_text("#!/bin/sh\nexec " + command + " curl " + shlex.quote(str(root)) + ' "$@"\n')
            wrapper.chmod(0o755)
            with Path(os.environ["GITHUB_ENV"]).open("a") as output:
                output.write(f"HCD_ROOT={root}\n")
            with Path(os.environ["GITHUB_PATH"]).open("a") as output:
                output.write(str(root / "bin") + "\n")
            with Path(os.environ["GITHUB_OUTPUT"]).open("a") as output:
                for key, value in (("version",state["version"]), ("cache_dir",state["cache_dir"]), ("cache_key",state["cache_key"])):
                    output.write(f"{key}={value}\n")
        except BaseException:
            shutil.rmtree(root)  # root created by this init only
            raise
    elif len(args) >= 2 and args[0] == "curl":
        def real_executor(command, env, timeout):
            return subprocess.run(command, env=env, stdin=subprocess.DEVNULL, stdout=subprocess.DEVNULL,
                                  stderr=subprocess.PIPE, timeout=timeout).returncode
        fetch(Path(args[1]), args[2:], real_executor)
    elif args == ["arm"]:
        root = Path(os.environ["HCD_ROOT"])
        require(shutil.which("wfctl") is None, "pre-existing wfctl on PATH")
        require(shutil.which("curl") == str(root / "bin/curl"), "fixture curl not first on PATH")
        require(str(Path(os.environ.get("RUNNER_TOOL_CACHE") or os.environ["RUNNER_TEMP"])) == load(root)["cache_root"], "actual runner cache root changed")
        arm(root)
    elif len(args) == 2 and args[0] == "observe":
        receipt = observe(Path(os.environ["HCD_ROOT"]), args[1])
        rendered = json.dumps(receipt, indent=2)
        print(rendered)
        with Path(os.environ["GITHUB_STEP_SUMMARY"]).open("a") as output:
            output.write("### Per-invocation path/byte observation (not full qualification)\n```json\n" + rendered + "\n```\n")
    elif args == ["cleanup"]:
        print(json.dumps(cleanup(Path(os.environ["HCD_ROOT"]))))
    else:
        raise ValueError("invalid fixture command")


if __name__ == "__main__":
    try:
        cli(sys.argv[1:])
    except (ValueError, OSError, KeyError, subprocess.TimeoutExpired) as error:
        # No raw arguments, environment, headers or transport stderr are echoed.
        print("hosted fixture failed: " + type(error).__name__ + (": " + str(error) if isinstance(error, ValueError) else ""), file=sys.stderr)
        sys.exit(90)
