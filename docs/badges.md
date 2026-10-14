# Badge snippets

## Local-action CI badge (copy-paste)

Paste into your repository's README once the workflow from
[adoption.md](adoption.md) is merged:

```markdown
[![docs-rot](https://github.com/OWNER/REPO/actions/workflows/docs-rot.yml/badge.svg)](https://github.com/OWNER/REPO/actions/workflows/docs-rot.yml)
```

Replace `OWNER/REPO`. The badge is green while `brokenTotal == 0` in fail
mode, and green-but-report-only while the gate is still warn-only (GitHub
shows workflow status, not the count).

## Status shield (static, no counter workflow)

For a shield that states the policy instead of the live count:

```markdown
[![docs-rot gate](https://img.shields.io/badge/docs-rot-CI%20gate-red)](https://github.com/OWNER/REPO/actions/workflows/docs-rot.yml)
```

## Live `brokenTotal` shield (planned)

A per-repository `broken-total` counter badge requires a tiny counter
workflow that publishes the last report's `brokenTotal` as a shields.io
endpoint or gist JSON. Not shipped yet — tracked in the hosted scanner's
backlog; the CI badge above is the source-only signal until then.
