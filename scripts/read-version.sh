# Source from the composite action. Keep traps and capture files in a subshell.
wfctl_read_version() (
  [ "$#" -eq 2 ] || exit 2
  wfctl_bin="$1"
  expected_version="$2"
  case "$wfctl_bin" in /*) ;; *) exit 2 ;; esac
  case "$expected_version" in ''|*$'\n'*|*$'\r'*) exit 2 ;; esac

  version_tmp="$(mktemp -d "${RUNNER_TEMP:?}/setup-wfctl-version.XXXXXX")" || exit 1
  cleanup_version_capture() {
    capture_status=$?
    rm -f -- "$version_tmp/stdout" "$version_tmp/stderr" "$version_tmp/expected" || {
      [ "$capture_status" -ne 0 ] || capture_status=1
    }
    rmdir -- "$version_tmp" || { [ "$capture_status" -ne 0 ] || capture_status=1; }
    exit "$capture_status"
  }
  trap cleanup_version_capture EXIT

  # Do not put the binary in a pipeline or hide its exit status with `|| true`.
  if WFCTL_NO_UPDATE_CHECK=1 "$wfctl_bin" version >"$version_tmp/stdout" 2>"$version_tmp/stderr"; then
    :
  else
    command_status=$?
    cat "$version_tmp/stdout" "$version_tmp/stderr" >&2 || :
    exit "$command_status"
  fi

  # Compare raw files, not shell-captured output: NULs, whitespace and duplicate
  # lines must not disappear during normalization. Permit only an optional LF.
  version_stream_matches() {
    printf '%s' "$2" >"$version_tmp/expected" || exit 1
    if cmp -s "$1" "$version_tmp/expected"; then
      return 0
    else
      [ "$?" -eq 1 ] || exit 1
    fi
    printf '%s\n' "$2" >"$version_tmp/expected" || exit 1
    if cmp -s "$1" "$version_tmp/expected"; then
      return 0
    else
      compare_status=$?
      [ "$compare_status" -eq 1 ] || exit 1
      return 1
    fi
  }

  # This exact diagnostic accompanies the released v0.87.0 CLI on Go 1.27.
  # It may occur once on stderr; arbitrary diagnostics remain a hard failure.
  sonic_warning='WARNING: sonic/ast only supports (go1.17~1.26 and amd64 CPU) or (go1.20~1.26 and arm64 CPU), but your environment is not suitable and will fallback to encoding/json'
  version_valid=false
  if version_stream_matches "$version_tmp/stdout" "$expected_version"; then
    if [ ! -s "$version_tmp/stderr" ] || version_stream_matches "$version_tmp/stderr" "$sonic_warning"; then
      version_valid=true
    fi
  elif [ ! -s "$version_tmp/stdout" ]; then
    if version_stream_matches "$version_tmp/stderr" "$expected_version" ||
       version_stream_matches "$version_tmp/stderr" "$sonic_warning"$'\n'"$expected_version" ||
       version_stream_matches "$version_tmp/stderr" "$expected_version"$'\n'"$sonic_warning"; then
      version_valid=true
    fi
  fi

  # Preserve the original stderr, including the known warning, in the job log.
  cat "$version_tmp/stderr" >&2 || exit 1
  if [ "$version_valid" != true ]; then
    cat "$version_tmp/stdout" >&2 || exit 1
    printf '%s\n' '::error::wfctl version output is missing, ambiguous or unexpected' >&2
    exit 1
  fi
  printf '%s\n' "$expected_version" || exit 1
)
