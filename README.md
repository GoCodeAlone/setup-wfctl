# setup-wfctl

A composite GitHub Action that installs [wfctl](https://github.com/GoCodeAlone/workflow), the CLI for the GoCodeAlone workflow engine.

## Usage

```yaml
steps:
  - uses: actions/checkout@v4
  - uses: GoCodeAlone/setup-wfctl@v1
  - run: wfctl infra plan -c infra.yaml
```

### With a specific version

```yaml
steps:
  - uses: actions/checkout@v4
  - uses: GoCodeAlone/setup-wfctl@v1
    with:
      version: 'v0.3.51'
  - run: wfctl validate -c app.yaml
```

### Full CI example

```yaml
name: Validate workflow config

on: [push, pull_request]

jobs:
  validate:
    runs-on: ubuntu-latest
    steps:
      - uses: actions/checkout@v4
      - uses: GoCodeAlone/setup-wfctl@v1
        with:
          version: 'latest'
          token: ${{ secrets.GITHUB_TOKEN }}
      - run: wfctl validate -c config/app.yaml
      - run: wfctl diff -c config/app.yaml --base main
```

## Inputs

| Input         | Description                                                                                                    | Required | Default                |
|---------------|----------------------------------------------------------------------------------------------------------------|----------|------------------------|
| `version`     | wfctl version to install (e.g. `v0.3.51`, `latest`)                                                           | No       | `latest`               |
| `token`       | GitHub token for downloading releases                                                                          | No       | `${{ github.token }}`  |
| `install-dir` | Directory to install `wfctl` into. When set, `sudo` is **not** used and the directory is added to `PATH`. Defaults to `/usr/local/bin` (existing behaviour). | No       | `` (uses `/usr/local/bin`) |
| `update-timeout-seconds` | Maximum time to allow an existing `wfctl` binary to self-update before falling back to release download. | No | `300` |
| `skip-update` | Set to `true` to skip `wfctl update` attempts when an existing binary does not match the requested version. | No | `false` |
| `download-timeout-seconds` | Maximum time for each release download request. | No | `60` |
| `download-connect-timeout-seconds` | Maximum time to establish each release download connection. | No | `30` |

## Self-hosted runners (no sudo)

On self-hosted runners where `sudo` is unavailable or intentionally blocked, set `install-dir` to a user-writable path:

```yaml
steps:
  - uses: actions/checkout@v4
  - uses: GoCodeAlone/setup-wfctl@v1
    with:
      install-dir: ${HOME}/.local/bin
  - run: wfctl version
```

The action will download `wfctl` directly into the specified directory, `chmod +x` it without `sudo`, and append the directory to `GITHUB_PATH` so subsequent steps can find the binary.

## Caching

Downloaded binaries are cached using `actions/cache@v5` keyed on version, OS, and architecture. Subsequent runs with the same version skip the download. The cache path reflects the resolved install directory.

## Existing wfctl binaries

When a runner already has `wfctl` on `PATH`, the action first checks its version.
If it already matches the requested version, the action reuses it and skips cache
restore and download. If it does not match, the action attempts a bounded
`wfctl update`; when the updated binary still does not match the requested
version, the action falls back to the verified release download path.

Set `skip-update: true` for pinned workflows that must not mutate the runner's
existing `wfctl` binary. In that mode, a mismatched existing binary is ignored
and the action uses the checksum-verified release download/cache path instead.

When `install-dir` is set and the existing `wfctl` on `PATH` lives elsewhere,
the action also skips `wfctl update` for that unrelated binary. This keeps
self-hosted workflows from attempting to mutate a global or unwritable `wfctl`
before installing the requested version into the workflow-owned directory.

## Verification

When `version: latest` is used, the action resolves and prints the concrete
Workflow release tag before downloading. Every download also fetches the
release `checksums.txt` file and verifies the selected `wfctl-${os}-${arch}`
asset before installing it. Download requests use bounded curl connection and
total request timeouts.

## License

MIT
