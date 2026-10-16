#!/usr/bin/env python3
"""Unit tests for scripts/idl_freshness.py.

Run from the repo root:
    python3 -m unittest scripts/test_idl_freshness.py
"""
import json
import os
import sys
import tempfile
import unittest
from unittest import mock

sys.path.insert(0, os.path.join(os.path.dirname(__file__), "..", "scripts"))

import idl_freshness  # noqa: E402

STREAMFLOW_DEVNET = "HqDGZjaVRXJ9MGRQEw7qDc2rAr6iH1n1kAQdCZaCMfMZ"
STREAMFLOW_MAINNET = "strmRqUCoQUgGUan5YhzUZa6KqdzwX5L6FpUxfmKg5m"
EXAMPLE_PROGRAM = "FGjLaVo5zLGdzCxMo9gu9tXr1kzTToKd8C8K7YS5hNM1"
SYSTEM_PROGRAM = "11111111111111111111111111111111"


def _write(tmp, rel, content):
    p = os.path.join(tmp, rel)
    os.makedirs(os.path.dirname(p), exist_ok=True)
    with open(p, "w", encoding="utf-8") as fh:
        fh.write(content)
    return p


class Base58Tests(unittest.TestCase):
    def test_valid_program_ids(self):
        for pid in (STREAMFLOW_DEVNET, STREAMFLOW_MAINNET,
                    EXAMPLE_PROGRAM, SYSTEM_PROGRAM):
            self.assertTrue(idl_freshness.is_valid_program_id(pid), pid)

    def test_invalid_program_ids(self):
        self.assertFalse(idl_freshness.is_valid_program_id("not-base58!"))
        self.assertFalse(idl_freshness.is_valid_program_id(""))
        self.assertFalse(idl_freshness.is_valid_program_id("abcd"))
        # 'X' is base58, '0' is not
        self.assertFalse(idl_freshness.is_valid_program_id("0" * 32))


class RustScanTests(unittest.TestCase):
    def test_real_streamflow_program(self):
        decls, err = idl_freshness.scan_rust_file(
            "/home/aurora/github_repos/rust-sdk/programs/streamflow-sdk/src/lib.rs",
            "programs/streamflow-sdk/src/lib.rs",
        )
        self.assertIsNone(err)
        ids = {d["programId"] for d in decls}
        self.assertIn(STREAMFLOW_DEVNET, ids)
        self.assertIn(STREAMFLOW_MAINNET, ids)
        # The devnet program is declared under cfg(feature = "devnet"),
        # the mainnet one under cfg(not(feature = "devnet")).
        by_cluster = {d["programId"]: d["cluster"] for d in decls}
        self.assertEqual(by_cluster[STREAMFLOW_DEVNET], "devnet")
        self.assertEqual(by_cluster[STREAMFLOW_MAINNET], "mainnet")

    def test_comments_are_ignored(self):
        with tempfile.TemporaryDirectory() as tmp:
            p = _write(tmp, "lib.rs",
                       'declare_id!("%s");\n'
                       '// declare_id!("%s")\n'
                       '/// declare_id!("%s")\n'
                       % (STREAMFLOW_DEVNET, STREAMFLOW_MAINNET,
                          EXAMPLE_PROGRAM))
            decls, err = idl_freshness.scan_rust_file(p, "lib.rs")
            self.assertIsNone(err)
            self.assertEqual(len(decls), 1)
            self.assertEqual(decls[0]["programId"], STREAMFLOW_DEVNET)

    def test_program_id_const(self):
        with tempfile.TemporaryDirectory() as tmp:
            p = _write(tmp, "lib.rs",
                       'pub const PROGRAM_ID: &str = "%s";\n' % STREAMFLOW_DEVNET)
            decls, _ = idl_freshness.scan_rust_file(p, "lib.rs")
            self.assertEqual(decls[0]["kind"], "program_id_const")
            self.assertEqual(decls[0]["programId"], STREAMFLOW_DEVNET)


class AnchorTomlScanTests(unittest.TestCase):
    TOML = (
        '[toolchain]\nanchor_version = "0.30.0"\n\n'
        '[programs.mainnet]\nstreamflow_sdk = "%s"\n\n'
        '[programs.devnet]\nstreamflow_sdk = "%s"\n'
    ) % (STREAMFLOW_MAINNET, STREAMFLOW_DEVNET)

    def test_parses_programs_table(self):
        with tempfile.TemporaryDirectory() as tmp:
            p = _write(tmp, "Anchor.toml", self.TOML)
            decls, err = idl_freshness.scan_anchor_toml(p, "Anchor.toml")
            self.assertIsNone(err)
            self.assertEqual(len(decls), 2)
            self.assertEqual({d["program"] for d in decls}, {"streamflow_sdk"})
            self.assertEqual({d["programId"] for d in decls},
                             {STREAMFLOW_MAINNET, STREAMFLOW_DEVNET})


class IdlScanTests(unittest.TestCase):
    def test_anchor_v0_1_address(self):
        with tempfile.TemporaryDirectory() as tmp:
            p = _write(tmp, "idl/x.json", json.dumps({
                "name": "x", "version": "0.1.0",
                "address": STREAMFLOW_DEVNET, "instructions": []}))
            decls, err = idl_freshness.scan_idl_json(p, "idl/x.json")
            self.assertIsNone(err)
            self.assertEqual(decls[0]["programId"], STREAMFLOW_DEVNET)

    def test_legacy_metadata_address(self):
        with tempfile.TemporaryDirectory() as tmp:
            p = _write(tmp, "idl/x.json", json.dumps({
                "name": "x",
                "metadata": {"address": STREAMFLOW_DEVNET}}))
            decls, _ = idl_freshness.scan_idl_json(p, "idl/x.json")
            self.assertEqual(decls[0]["programId"], STREAMFLOW_DEVNET)

    def test_unparsable_idl(self):
        with tempfile.TemporaryDirectory() as tmp:
            p = _write(tmp, "idl/x.json", "not json")
            decls, err = idl_freshness.scan_idl_json(p, "idl/x.json")
            self.assertEqual(decls, [])
            self.assertIn("unparsable IDL", err)


class RotDetectionTests(unittest.TestCase):
    def test_declared_id_drift(self):
        # Anchor.toml devnet entry has STREAMFLOW_DEVNET but the only
        # declare_id! in the repo is STREAMFLOW_MAINNET -> drift.
        declarations = [
            {"file": "lib.rs", "line": 1, "kind": "declare_id",
             "programId": STREAMFLOW_MAINNET},
            {"file": "Anchor.toml", "line": 5, "kind": "anchor_toml",
             "programId": STREAMFLOW_DEVNET, "program": "streamflow_sdk"},
        ]
        findings = idl_freshness.find_rot(declarations, {})
        kinds = {f["kind"] for f in findings}
        self.assertIn("declared-id-drift", kinds)

    def test_invalid_program_id(self):
        declarations = [
            {"file": "idl/moved.json", "line": 1, "kind": "idl_address",
             "programId": "0" * 32},
        ]
        findings = idl_freshness.find_rot(declarations, {})
        self.assertEqual(findings[0]["kind"], "invalid-program-id")

    def test_program_not_found_on_chain(self):
        declarations = [
            {"file": "lib.rs", "line": 1, "kind": "declare_id",
             "programId": STREAMFLOW_MAINNET, "cluster": "mainnet"},
        ]
        onchain = {STREAMFLOW_MAINNET: {"exists": False, "executable": False,
                                        "cluster": "mainnet"}}
        findings = idl_freshness.find_rot(declarations, onchain)
        self.assertEqual(findings[0]["kind"], "program-not-found")
        self.assertIn("mainnet", findings[0]["message"])

    def test_executable(self):
        declarations = [
            {"file": "lib.rs", "line": 1, "kind": "declare_id",
             "programId": STREAMFLOW_MAINNET, "cluster": "mainnet"},
        ]
        onchain = {STREAMFLOW_MAINNET: {"exists": True, "executable": True,
                                        "cluster": "mainnet"}}
        self.assertEqual(idl_freshness.find_rot(declarations, onchain), [])


class UnresolvableTests(unittest.TestCase):
    def test_rpc_error_does_not_count_as_rot(self):
        report = idl_freshness.build_report(
            "/", [], {"rustFiles": 0, "anchorToml": 0, "idlJson": 0},
            [], {}, "rpc unreachable: HTTP 429", "mainnet-beta")
        self.assertEqual(report["rotTotal"], 0)
        self.assertEqual(report["unresolvableTotal"], 1)
        self.assertEqual(report["unresolvable"][0]["reason"],
                         "rpc unreachable: HTTP 429")


class CliTests(unittest.TestCase):
    def _clean_tree(self):
        tmp = tempfile.mkdtemp()
        _write(tmp, "lib.rs", 'declare_id!("%s");\n' % STREAMFLOW_MAINNET)
        _write(tmp, "Anchor.toml",
               '[programs.mainnet]\nfoo = "%s"\n' % STREAMFLOW_MAINNET)
        return tmp

    def test_clean_offline_exits_zero(self):
        tmp = self._clean_tree()
        report = "/tmp/idl-freshness-clean.json"
        try:
            rc = idl_freshness.main([tmp, "--report", report,
                                     "--offline", "--fail-on-rot"])
            self.assertEqual(rc, 0)
            data = json.load(open(report))
            self.assertEqual(data["rotTotal"], 0)
        finally:
            os.unlink(report)

    def test_fail_on_rot_exits_three(self):
        tmp = tempfile.mkdtemp()
        _write(tmp, "lib.rs", 'declare_id!("%s");\n' % STREAMFLOW_MAINNET)
        _write(tmp, "idl/moved.json", json.dumps(
            {"name": "x", "address": "0" * 32}))
        report = "/tmp/idl-freshness-rot.json"
        try:
            rc = idl_freshness.main([tmp, "--report", report,
                                     "--offline", "--fail-on-rot"])
            self.assertEqual(rc, 3)
            data = json.load(open(report))
            self.assertGreaterEqual(data["rotTotal"], 1)
        finally:
            os.unlink(report)


if __name__ == "__main__":
    unittest.main()
