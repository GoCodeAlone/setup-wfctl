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

## License

MIT
