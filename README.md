# docrot-check

A GitHub Action that scans a repository's markdown documentation for **dead
image references** and fails (or warns) when it finds them.

Two classes of rot are detected:

- **Broken external images** — hot-linked screenshots/badges hosted outside the
  repository whose target now returns HTTP >= 400 or nothing at all.
- **Missing in-repo files** — image paths that name a file that no longer
  exists in the tree.

Unresolvable references (template placeholders, docs-site root-relatives) are
reported separately and never count as broken, so the check is safe to run on
repositories that use a static-site generator.

Engine: [auroraxo/docrot](https://github.com/auroraxo/docrot) (`scan_local.py`),
checked out automatically by this action. No GitHub token is required and the
only network traffic is the URL liveness checks themselves.

## Usage

```yaml
name: Docs health

on:
  push:
    branches: [main]
  schedule:
    - cron: "17 6 * * 1"   # weekly: link rot appears without any commit

jobs:
  docrot:
    runs-on: ubuntu-latest
    steps:
      - uses: actions/checkout@v4
      - uses: auroraxo/docrot-check@v1
        with:
          fail-on-broken: "true"
```

By default (`fail-on-broken: "false"`) the step reports findings in the job
summary and as an artifact without failing the build — a good first week before
turning it into a hard gate.

## Inputs

| Input | Default | Description |
| --- | --- | --- |
| `root` | `.` | Repository root to scan, relative to the workspace. |
| `fail-on-broken` | `false` | Exit non-zero when a broken reference is found. |
| `report-path` | `docrot-report.json` | Where the JSON report is written. |
| `upload-report` | `true` | Upload the report as artifact `docrot-report`. |

## Outputs

| Output | Description |
| --- | --- |
| `broken-total` | Number of confirmed dead references (external broken + missing in-repo). |
| `report-path` | Path to the JSON report. |

## What the report contains

The JSON report lists every image reference with its classification
(`in-repo`, `missing`, `external`, `unresolvable`), the HTTP status of
external targets, and the markdown files each reference appears in — enough to
open a fix PR or file an upstream issue directly from CI.

## License

MIT — see [LICENSE](LICENSE).
