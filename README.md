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

| Input     | Description                                          | Required | Default                |
|-----------|------------------------------------------------------|----------|------------------------|
| `version` | wfctl version to install (e.g. `v0.3.51`, `latest`) | No       | `latest`               |
| `token`   | GitHub token for downloading releases                | No       | `${{ github.token }}`  |

## Caching

Downloaded binaries are cached using `actions/cache@v4` keyed on version, OS, and architecture. Subsequent runs with the same version skip the download.

## License

MIT
