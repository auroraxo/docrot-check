#!/usr/bin/env python3
"""open_rot_issues.py — turn downloaded docrot-check reports into labelled issues.

Contract: open at most one open issue per target per repo at a time. A target
is "newly rotten" when brokenTotal >= 1 AND no open `docs-rot`-labelled issue
already exists for that target. Findings are serialised to a fenced `json`
block so a human or downstream tool can parse them back out. The issue body
is built from the per-finding evidence in the report (HTTP status, host,
markdown ref, kind) and is intentionally short — a fix PR is the follow-up,
not a wall of evidence.

Idempotency: before opening, we list currently-open `docs-rot` issues in the
target repo and skip any target that already has one open. This makes the
scheduled 2x/week cron safe to run against a target list: the first positive
scan opens the issue, subsequent positive runs only re-open once the
previous one is closed. Daily-run noise protection is still the caller's
job (we still only run 2x/week). To force a refresh, close the existing
issue and re-dispatch; the scanner will open a new one.
"""
import json
import os
import subprocess
import sys
import tempfile
from contextlib import closing
from pathlib import Path


def gh(args, env=None):
    e = dict(os.environ)
    if env:
        e.update(env)
    return subprocess.check_output(["gh", *args], env=e, text=True)


def short_sha(target):
    return target.replace("/", "-")


def open_docs_rot_targets(repo):
    """Return the set of targets (owner/repo) that already have an OPEN
    `docs-rot`-labelled issue in the given repo. Used to dedup before we
    open a new one. We match on the issue title prefix `docs-rot: <sha>-`
    which is the same shape `build_title` produces. Closed issues do not
    block — closing is the explicit "start over" signal."""
    out = gh([
        "issue", "list",
        "--repo", repo,
        "--label", "docs-rot",
        "--state", "open",
        "--limit", "100",
        "--json", "title",
    ])
    try:
        issues = json.loads(out)
    except ValueError:
        return set()
    tracked = set()
    prefix = "docs-rot: "
    for it in issues:
        title = it.get("title", "")
        if not title.startswith(prefix):
            continue
        # docs-rot: owner-repo — N dead reference(s)
        # We want the "owner-repo" portion as the dedup key.
        rest = title[len(prefix):]
        sha = rest.split(" — ", 1)[0].strip()
        if sha:
            tracked.add(sha)
    return tracked


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
        "Bot opens at most one OPEN `docs-rot` issue per target per repo. ",
        "Subsequent positive runs are a no-op while that issue is open; ",
        "close it to refresh. Use `not-rot` if the finding is actually ",
        "unresolvable (template placeholders, docs-site root-relatives).",
    ])
    body = "\n".join(lines)
    # GitHub caps issue bodies at 65,536 characters (GraphQL createIssue).
    # A full report can be >100KB (nocodb: 135KB), so when the body is over
    # the limit, swap the embedded raw report for a trimmed variant: broken
    # findings only first, summary only second, then an honest truncation
    # marker. The scan artifact remains the full source of truth either way.
    limit = 65_000
    if len(body) > limit:
        body = trim_block(lines, limit)
        if len(body) > limit:
            marker = "\n<!-- body truncated by open_rot_issues.py; see run artifact -->\n"
            body = body[: limit - len(marker)] + marker
    return body


def trim_block(lines, limit):
    """Rebuild lines with the fenced raw-report block replaced by a smaller
    variant (broken findings only, then summary only). Returns the joined body."""
    out = lines[:]
    # locate the fenced json block
    try:
        start = out.index("```json")
    except ValueError:
        return "\n".join(out)
    end = out.index("```", start + 1)
    block_lines = out[start + 1:end]
    raw = "\n".join(block_lines)
    try:
        report = json.loads(raw)
    except ValueError:
        return "\n".join(out)
    head = out[:start]
    tail = out[end + 1:]
    variants = [
        report_broken_only(report),
        report_summary(report),
    ]
    for v in variants:
        candidate = head + [json.dumps(v, indent=1)] + tail
        joined = "\n".join(candidate)
        if len(joined) <= limit:
            return joined
    # Last resort: summary without images at all, then hard truncation by caller.
    return "\n".join(head + [json.dumps(report_summary(report), indent=1)] + tail)


def report_broken_only(report):
    r = dict(report)
    r["images"] = [i for i in report.get("images", []) if i.get("broken")]
    r["trimmed"] = "full images[] omitted; broken findings only"
    return r


def report_summary(report):
    return {k: v for k, v in report.items() if k != "images"} | {
        "trimmed": "images[] omitted; see run artifact for the full report"
    }


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
    host_repo = os.environ.get("GH_REPO", "auroraxo/docrot-check")
    # Resolve the set of targets that already have an open docs-rot issue
    # in the host repo so we don't spam duplicates on recurring positive
    # scans. The list is computed once per decide job; the API call is a
    # single `gh issue list` (capped at 100, well above our target count).
    try:
        already_tracked = open_docs_rot_targets(host_repo)
    except subprocess.CalledProcessError as e:
        # If gh fails (auth, network), do not silently open everything —
        # abort so the run surfaces the error and retries next cycle.
        print(f"error: could not list open docs-rot issues: {e}", file=sys.stderr)
        sys.exit(1)
    for p in files:
        with closing(open(p)) as fh:
            report = json.load(fh)
        target = report.get("target") or "?"
        broken = report.get("brokenTotal", 0)
        if not target or target == "?":
            print(f"skip {p.name}: no target")
            continue
        if broken < 1:
            print(f"skip {target}: brokenTotal=0 (negativtest passed)")
            continue
        sha = short_sha(target)
        if sha in already_tracked:
            print(f"skip {target}: open docs-rot issue already exists (sha={sha})")
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
