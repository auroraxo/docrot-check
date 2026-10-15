#!/usr/bin/env python3
"""open_rot_issues.py — turn downloaded docrot-check reports into labelled issues.

Contract: open at most one issue per target per workflow run. A target is
"newly rotten" when brokenTotal >= 1. Findings are serialised to a fenced
`json` block so a human or downstream tool can parse them back out. The
issue body is built from the per-finding evidence in the report (HTTP status,
host, markdown ref, kind) and is intentionally short — a fix PR is the
follow-up, not a wall of evidence.

Idempotency: the workflow label `docs-rot` plus the run-id is enough to make
runs visible to humans; opening is rate-bounded by the 2x/week schedule.
Re-runs of the same workflow run (e.g. retrying the decide job) will only
open a fresh issue if a previous attempt was killed before the issue opened,
which is the safe default. Daily-run noise protection is the caller's job.
"""
import json
import os
import subprocess
import sys
import tempfile
from pathlib import Path


def gh(args, env=None):
    e = dict(os.environ)
    if env:
        e.update(env)
    return subprocess.check_output(["gh", *args], env=e, text=True)


def short_sha(target):
    return target.replace("/", "-")


def build_body(report, run_id):
    tool = report.get("tool", "docrot-scan-local")
    version = report.get("version", "?")
    generated = report.get("generatedAt", "")
    host = os.environ.get("GH_REPO", "auroraxo/docrot-check")
    findings = [i for i in report.get("images", []) if i.get("broken")]
    lines = [
        f"### docs-rot findings for `{report.get('target')}`",
        "",
        f"- run: `{run_id}` (workflow `weekly-docs-rot.yml` in `{host}`)",
        f"- engine: `{tool}` v{version}",
        f"- generated: `{generated}`",
        f"- markdown files scanned: **{report.get('markdownFilesScanned', 0)}**",
        f"- image references: **{report.get('imageRefs', 0)}**",
        f"- unique images: **{report.get('uniqueImages', 0)}**",
        f"- broken total (this run): **{report.get('brokenTotal', 0)}**",
        "",
        "### Findings",
        "",
    ]
    if not findings:
        lines.append("No broken references in this run.")
    else:
        for f in findings:
            kind = f.get("kind", "?")
            target = f.get("target") or f.get("originalRef", "?")
            note = f.get("note") or f"status={f.get('status')}"
            ref_lines = []
            for path, src in f.get("refs", []):
                ref_lines.append(f"  - `{path}` -> `{src}`")
            lines.append(f"- **[{kind}]** `{target}` ({note})")
            lines.extend(ref_lines)
    lines.extend([
        "",
        "### Raw report",
        "",
        "```json",
        json.dumps(report, indent=1),
        "```",
        "",
        "Bot opens at most one issue per target per run. Re-run is the manual",
        "way to refresh; close with `not-rot` if the finding is actually",
        "unresolvable (template placeholders, docs-site root-relatives).",
    ])
    return "\n".join(lines)


def build_title(target, broken_total):
    return f"docs-rot: {short_sha(target)} — {broken_total} dead reference(s)"


def main():
    if len(sys.argv) < 2:
        print("usage: open_rot_issues.py <reports-dir>", file=sys.stderr)
        sys.exit(2)
    reports_dir = Path(sys.argv[1])
    files = sorted(p for p in reports_dir.glob("*.json"))
    if not files:
        print(f"no reports in {reports_dir}")
        return
    run_id = os.environ.get("GITHUB_RUN_ID") or "local"
    for p in files:
        report = json.load(open(p))
        target = report.get("target") or "?"
        broken = report.get("brokenTotal", 0)
        if not target or target == "?":
            print(f"skip {p.name}: no target")
            continue
        if broken < 1:
            print(f"skip {target}: brokenTotal=0 (negativtest passed)")
            continue
        body = build_body(report, run_id)
        title = build_title(target, broken)
        # Body can be large (raw report embedded). Linux exec caps a single
        # argv string at MAX_ARG_STRLEN (~128KiB); `gh issue create --body`
        # would die with E2BIG ("Argument list too long") and fail the decide
        # job after the scan succeeded. Pass the body via a temp file instead:
        # --body-file streams it from gh's own file read, no argv limit.
        if len(body.encode()) > 100_000:
            print(f"warn: {target} body {len(body.encode())} bytes (large report)", file=sys.stderr)
        with tempfile.NamedTemporaryFile("w", suffix=".md", delete=False) as tf:
            tf.write(body)
            body_path = tf.name
        try:
            # Quiet pre-check so we never log a token.
            out = gh([
                "issue", "create",
                "--repo", os.environ.get("GH_REPO", "auroraxo/docrot-check"),
                "--title", title,
                "--body-file", body_path,
                "--label", "docs-rot",
            ])
        finally:
            Path(body_path).unlink(missing_ok=True)
        print(f"opened issue for {target} (broken={broken}) -> {out.strip()}")


if __name__ == "__main__":
    main()
