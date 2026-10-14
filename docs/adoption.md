# Adopting docrot-check in a repository

This guide gets the [docrot-check](https://github.com/auroraxo/docrot-check)
GitHub Action running in your repository in under five minutes. It is the
companion to the hosted weekly scanner in this repository's
`weekly-docs-rot.yml` workflow — if you want the same findings without
configuring anything, paste your target into
`auroraxo/docrot-check`'s `.github/docrot-targets.txt` and wait for the
next Mon/Thu 06:17Z window.

## 1. Add the workflow

Copy the following file to `.github/workflows/docs-rot.yml` in your
repository:

```yaml
name: docs-rot
on:
  push:
    branches: [main]
  pull_request:
  schedule:
    - cron: "17 6 * * 1"   # weekly; link rot appears without any commit
  workflow_dispatch:

permissions:
  contents: read

jobs:
  scan:
    runs-on: ubuntu-latest
    steps:
      - uses: actions/checkout@v4
      - name: docrot-check (warn)
        uses: auroraxo/docrot-check@v1
        with:
          root: .
          fail-on-broken: "false"   # report only — flip to "true" once clean
          report-path: docrot-report.json
          upload-report: "true"
```

## 2. Pin to a SHA before going to fail mode

While `v1` is the published tag, pin to a specific commit SHA in production
and update the SHA deliberately:

```yaml
- uses: auroraxo/docrot-check@<40-char-sha>
```

This avoids surprise breaks on a tag move. The action's own `.github/workflows/ci.yml`
demonstrates the pinned version.

## 3. Read the report

Every run uploads `docrot-report` (JSON) and writes a headline table to the
job summary. The fields you care about are:

| Field | Meaning |
| --- | --- |
| `markdownFilesScanned` | How many `.md`/`.mdx` files were inspected. |
| `imageRefs` | Total `<img>` / `![]()` references found. |
| `uniqueImages` | Distinct image targets. |
| `externalThirdParty` | References hosted outside the repository. |
| `externalThirdPartyBroken` | Hot-linked images returning HTTP >= 400. |
| `missingInRepo` | Local image paths with no file in the tree. |
| `unresolvable` | Template placeholders / docs-site root-relatives (never count as rot). |
| `brokenTotal` | `externalThirdPartyBroken` + `missingInRepo`. |

A clean repository has `brokenTotal == 0` and the job is green.

## 4. Going to fail mode

When a repository has gone a week with `brokenTotal == 0` in the warn run,
flip the gate:

```yaml
        with:
          fail-on-broken: "true"
```

That makes every PR that introduces a new broken reference fail CI. A
revert-style fix is `s/images/old.png/images/new.png/` and re-run.

## 5. Status badge (optional)

A simple Markdown snippet you can paste into README:

```markdown
[![docs-rot](https://github.com/OWNER/REPO/actions/workflows/docs-rot.yml/badge.svg)](https://github.com/OWNER/REPO/actions/workflows/docs-rot.yml)
```

The hosted weekly scanner does not yet expose a per-repository `brokenTotal`
badge — the action repo would need a separate counter workflow to publish
that. The CI badge above is the lightweight, source-only signal.

## 6. What the action does *not* do

- It does not push fixes, edit `README.md`, or open PRs on your behalf.
- It does not check non-image references (links, anchors, video tags).
- It does not require a GitHub token. The only network traffic is the
  external image liveness checks themselves.
- It does not run on third-party CI by default. It targets the canonical
  GitHub Actions environment.

## 7. Troubleshooting

- "All references are `unresolvable`" — the action cannot walk static-site
  generator output. Run the scan against the `docs/` source directory, not
  the built site.
- "Badges return 404" — the badge host may be blocking CI runners. Add
  `img.shields.io` to your allowlist or replace the badge with text.
- "I need a scope smaller than the whole repo" — pass `root: docs/`.

## License

MIT — see [LICENSE](../LICENSE). Powered by the [`auroraxo/docrot`](https://github.com/auroraxo/docrot)
scanner.
