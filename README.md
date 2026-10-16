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

## IDL freshness (opt-in)

When `check-idl-freshness: "true"`, the action additionally runs
`scripts/idl_freshness.py`, which crawls the repository for Solana/Anchor
program declarations and compares them to the on-chain account on the
configured cluster via JSON-RPC:

- `declare_id!("...")` in `*.rs` files (comment lines are ignored, the
  `#[cfg(feature = "devnet")]` / `#[cfg(not(feature = "devnet"))]` attribute
  is honoured so devnet programs are checked against devnet)
- `pub const PROGRAM_ID = "..."` in `*.rs` files
- the `[programs]` table of any `Anchor.toml`
- the `address` (or `metadata.address`) field of every Anchor IDL under
  `idl/` (including `target/idl`)

Three classes of rot are reported and never overlap with `unresolvable`:

1. **declared-id-drift** — an `idl/*.json` or `Anchor.toml` entry references a
   program ID that no `declare_id!` in the same repository declares.
2. **invalid-program-id** — a declaration is not a valid base58/32-byte Solana
   address.
3. **program-not-found / not-executable** — the on-chain `getAccountInfo`
   reply says the account does not exist (or is not an executable program).

Unresolvable checks (RPC unreachable, rate limited) are reported separately
and never count as rot, mirroring the image-refresh check's policy. Run the
unit tests with `python3 -m unittest scripts/test_idl_freshness.py`.

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
| `check-idl-freshness` | `false` | Run the Solana program / IDL freshness scan (`scripts/idl_freshness.py`). |
| `idl-cluster` | `mainnet` | Solana cluster for the freshness check: `mainnet`, `devnet`, or a full JSON-RPC URL. |
| `idl-fail-on-rot` | `false` | Exit non-zero when IDL rot is found. |
| `idl-report-path` | `idl-freshness-report.json` | Where the IDL freshness JSON report is written. |
| `report-path` | `docrot-report.json` | Where the JSON report is written. |
| `upload-report` | `true` | Upload the report as artifact `docrot-report`. |

## Outputs

| Output | Description |
| --- | --- |
| `broken-total` | Number of confirmed dead references (external broken + missing in-repo). |
| `report-path` | Path to the JSON report. |
| `idl-rot-total` | Number of confirmed IDL / program-ID rot findings (only emitted when `check-idl-freshness` is true). |
| `idl-report-path` | Path to the IDL freshness JSON report (only emitted when `check-idl-freshness` is true). |

## What the report contains

The JSON report lists every image reference with its classification
(`in-repo`, `missing`, `external`, `unresolvable`), the HTTP status of
external targets, and the markdown files each reference appears in — enough to
open a fix PR or file an upstream issue directly from CI.

## License

MIT — see [LICENSE](LICENSE).
