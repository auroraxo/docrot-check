#!/usr/bin/env python3
"""Unit tests for open_rot_issues.py dedup logic.

We can't shell out to `gh` in unit tests, so we stub subprocess.check_output
and exercise the pure-Python pieces: `open_docs_rot_targets` parses the issue
list JSON the same way the real call returns, and `build_title` /
`short_sha` round-trip the dedup key. The end-to-end dedup behaviour is
then verified by the hosted workflow run.

Run from the repo root: python3 -m unittest scripts/test_open_rot_issues.py
"""
import json
import os
import sys
import unittest
from unittest import mock

# Make the script importable as a module.
sys.path.insert(0, os.path.join(os.path.dirname(__file__), "..", "scripts"))

import open_rot_issues  # noqa: E402


class DedupParseTests(unittest.TestCase):
    def test_short_sha_uses_dash(self):
        self.assertEqual(open_rot_issues.short_sha("nocodb/nocodb"), "nocodb-nocodb")
        self.assertEqual(open_rot_issues.short_sha("minio/minio-go"), "minio-minio-go")

    def test_build_title_shape(self):
        t = open_rot_issues.build_title("nocodb/nocodb", 4)
        self.assertEqual(t, "docs-rot: nocodb-nocodb — 4 dead reference(s)")

    def test_open_docs_rot_targets_parses_open_issues(self):
        fake = json.dumps([
            {"title": "docs-rot: nocodb-nocodb — 4 dead reference(s)"},
            {"title": "docs-rot: bulletphysics-bullet3 — 2 dead reference(s)"},
        ])
        with mock.patch.object(open_rot_issues, "gh", return_value=fake):
            tracked = open_rot_issues.open_docs_rot_targets("auroraxo/docrot-check")
        self.assertEqual(
            tracked,
            {"nocodb-nocodb", "bulletphysics-bullet3"},
        )

    def test_open_docs_rot_targets_ignores_unrelated_titles(self):
        fake = json.dumps([
            {"title": "Q: what should a weekly docs-rot scan catch that link-checkers do not?"},
            {"title": "Seed receipt: weekly docs-rot scan, 2026-10-14 (minio/minio-go)"},
            {"title": "docs-rot: nocodb-nocodb — 4 dead reference(s)"},
        ])
        with mock.patch.object(open_rot_issues, "gh", return_value=fake):
            tracked = open_rot_issues.open_docs_rot_targets("auroraxo/docrot-check")
        # Only the matching-prefix title is tracked; "Seed receipt: docs-rot..."
        # has a different prefix and must not block a minio re-open.
        self.assertEqual(tracked, {"nocodb-nocodb"})

    def test_open_docs_rot_targets_handles_garbage(self):
        with mock.patch.object(open_rot_issues, "gh", return_value="not json"):
            tracked = open_rot_issues.open_docs_rot_targets("auroraxo/docrot-check")
        # Defensive: bad input must not crash; we treat it as "nothing tracked"
        # so the script proceeds to open (worst case: one duplicate, surfaces
        # in review). Better than silently blocking every target.
        self.assertEqual(tracked, set())


class DecideMainTests(unittest.TestCase):
    """Verify the per-target skip logic in main() with a stubbed `gh` that
    returns a known open-issues list. We don't actually open anything —
    `gh issue create` is mocked to assert the call shape, then we run a
    second pass and assert the call is skipped."""

    def _write_report(self, tmp, target, broken):
        p = tmp / f"{target.replace('/', '-')}.json"
        p.write_text(json.dumps({
            "target": target,
            "brokenTotal": broken,
            "markdownFilesScanned": 1,
            "imageRefs": 1,
            "uniqueImages": 1,
            "images": [],
        }))
        return p

    def test_skips_when_already_tracked(self):
        import tempfile
        with tempfile.TemporaryDirectory() as td:
            from pathlib import Path
            tmp = Path(td)
            self._write_report(tmp, "nocodb/nocodb", 4)
            self._write_report(tmp, "minio/minio-go", 0)

            create_calls = []

            def fake_gh(args, env=None):
                if args and args[0] == "issue" and args[1] == "list":
                    return json.dumps([
                        {"title": "docs-rot: nocodb-nocodb — 4 dead reference(s)"},
                    ])
                if args and args[0] == "issue" and args[1] == "create":
                    create_calls.append(args)
                    return "https://github.com/auroraxo/docrot-check/issues/42"
                raise AssertionError(f"unexpected gh call: {args}")

            with mock.patch.object(open_rot_issues, "gh", side_effect=fake_gh), \
                 mock.patch.dict(os.environ, {"GH_REPO": "auroraxo/docrot-check"}):
                # The real main() reads from sys.argv; pass our tmp dir.
                old_argv = sys.argv
                sys.argv = ["open_rot_issues.py", str(tmp)]
                try:
                    open_rot_issues.main()
                finally:
                    sys.argv = old_argv

            # minio/minio-go was clean -> skip. nocodb/nocodb was already
            # tracked -> skip. No `issue create` call should fire.
            self.assertEqual(create_calls, [])

    def test_opens_when_new_target(self):
        import tempfile
        from pathlib import Path
        with tempfile.TemporaryDirectory() as td:
            tmp = Path(td)
            self._write_report(tmp, "fresh-co/fresh-repo", 2)

            create_calls = []

            def fake_gh(args, env=None):
                if args and args[0] == "issue" and args[1] == "list":
                    return json.dumps([])  # nothing currently open
                if args and args[0] == "issue" and args[1] == "create":
                    create_calls.append(args)
                    return "https://github.com/auroraxo/docrot-check/issues/99"
                raise AssertionError(f"unexpected gh call: {args}")

            with mock.patch.object(open_rot_issues, "gh", side_effect=fake_gh), \
                 mock.patch.dict(os.environ, {"GH_REPO": "auroraxo/docrot-check"}):
                old_argv = sys.argv
                sys.argv = ["open_rot_issues.py", str(tmp)]
                try:
                    open_rot_issues.main()
                finally:
                    sys.argv = old_argv

            self.assertEqual(len(create_calls), 1)
            self.assertIn("--label", create_calls[0])
            self.assertIn("docs-rot", create_calls[0])
            self.assertIn("fresh-co-fresh-repo", create_calls[0][create_calls[0].index("--title") + 1])


if __name__ == "__main__":
    unittest.main()
