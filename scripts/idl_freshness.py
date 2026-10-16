#!/usr/bin/env python3
"""Solana program / IDL freshness check (v0.1).

Crawls a repository for every place a Solana/Anchor program ID is declared:

  - ``declare_id!(...)`` and ``pub const PROGRAM_ID`` in ``*.rs`` files
    (comment lines are skipped),
  - the ``[programs]`` table in ``Anchor.toml``,
  - the ``address`` field (or ``metadata.address``) of Anchor IDL ``*.json``
    files under an ``idl/`` directory (including ``target/idl``).

Two classes of rot are reported:

  - **Declared-ID drift** — an IDL/Anchor.toml program ID that no ``declare_id!``
    in the same repository declares (the on-chain program moved, the repo
    did not notice).
  - **On-chain rot** — a declared program ID that does not exist as an
    executable program account on the target cluster, checked via JSON-RPC
    ``getAccountInfo``.

Unresolvable checks (RPC unreachable, rate limited, timeouts) are reported
separately and never count as rot, mirroring docrot's unresolvable policy,
so the check is safe to run on a schedule.

Usage:
    python3 idl_freshness.py <root> [--report PATH] [--cluster mainnet|devnet|URL]
                              [--offline] [--fail-on-rot]

Exit status: 0 normally, 3 when ``--fail-on-rot`` is set and rot was found,
2 on usage errors. Unresolvable checks never affect the exit status.

Run the tests from the repo root:
    python3 -m unittest scripts/test_idl_freshness.py
"""
import argparse
import json
import os
import re
import sys
import urllib.error
import urllib.request

MAINNET_RPC = "https://api.mainnet-beta.solana.com"
DEVNET_RPC = "https://api.devnet.solana.com"

SKIP_DIRS = {".git", "node_modules", "__pycache__", ".docrot-tool", ".venv"}

BASE58_ALPHABET = "123456789ABCDEFGHJKLMNPQRSTUVWXYZabcdefghijkmnopqrstuvwxyz"
BASE58_INDEX = {c: i for i, c in enumerate(BASE58_ALPHABET)}

DECLARE_ID_RE = re.compile(r'declare_id!\s*\(\s*"([1-9A-HJ-NP-Za-km-z]+)"\s*\)')
PROGRAM_ID_CONST_RE = re.compile(
    r'pub\s+const\s+PROGRAM_ID[^=]*=\s*"([1-9A-HJ-NP-Za-km-z]+)"'
)
ANCHOR_TOML_PROGRAM_RE = re.compile(
    r'^\s*"?([A-Za-z0-9_-]+)"?\s*=\s*"([1-9A-HJ-NP-Za-km-z]+)"\s*$'
)
# Matches `#[cfg(feature = "devnet")]` -> declaration lives on devnet.
CFG_FEATURE_DEVNET_RE = re.compile(r'#\[cfg\(feature\s*=\s*"devnet"\)\]')
# Matches `#[cfg(not(feature = "devnet"))]` -> mainnet/default build.
CFG_NOT_FEATURE_DEVNET_RE = re.compile(r'#\[cfg\(not\(feature\s*=\s*"devnet"\)\)\]')


def b58decode(s):
    """Decode a base58 string to bytes; return None when it is not valid."""
    if not s:
        return None
    value = 0
    for ch in s:
        if ch not in BASE58_INDEX:
            return None
        value = value * 58 + BASE58_INDEX[ch]
    raw = value.to_bytes((value.bit_length() + 7) // 8, "big") if value else b""
    # Leading '1's are zero bytes.
    pad = len(s) - len(s.lstrip("1"))
    out = b"\x00" * pad + raw
    return out


def is_valid_program_id(s):
    return b58decode(s) is not None and len(b58decode(s)) == 32


def _code_lines(text):
    """Yield (line_number, line) for lines that are not comments."""
    for n, line in enumerate(text.splitlines(), 1):
        stripped = line.lstrip()
        if stripped.startswith("//"):
            continue
        yield n, line


def _cfg_cluster(*lines):
    """Cluster a declare_id! belongs to, from its cfg attribute.

    ``#[cfg(feature = "devnet")]`` -> "devnet",
    ``#[cfg(not(feature = "devnet"))]`` -> "mainnet", otherwise None
    (the caller's --cluster default applies).
    """
    for line in lines:
        if CFG_FEATURE_DEVNET_RE.search(line):
            return "devnet"
        if CFG_NOT_FEATURE_DEVNET_RE.search(line):
            return "mainnet"
    return None


def scan_rust_file(path, rel):
    """Return declarations found in one Rust source file."""
    found = []
    try:
        with open(path, encoding="utf-8", errors="replace") as fh:
            text = fh.read()
    except OSError as exc:
        return found, "unreadable: %s" % exc
    code = list(_code_lines(text))
    for idx, (lineno, line) in enumerate(code):
        prev = [code[idx - 1][1]] if idx else []
        # A declare_id! after code on the same line is still live code;
        # only full-line comments were filtered above.
        for regex, kind in ((DECLARE_ID_RE, "declare_id"),
                            (PROGRAM_ID_CONST_RE, "program_id_const")):
            for match in regex.finditer(line):
                found.append({
                    "file": rel, "line": lineno, "kind": kind,
                    "programId": match.group(1),
                    "cluster": _cfg_cluster(line, *prev),
                })
    return found, None


def scan_anchor_toml(path, rel):
    """Return declarations from Anchor.toml's [programs] tables."""
    found = []
    try:
        with open(path, encoding="utf-8", errors="replace") as fh:
            lines = fh.readlines()
    except OSError as exc:
        return found, "unreadable: %s" % exc
    section = None
    for lineno, line in enumerate(lines, 1):
        stripped = line.split("#", 1)[0].rstrip()
        if stripped.startswith("[") and stripped.endswith("]"):
            section = stripped[1:-1].strip()
            continue
        if section is None or not section.startswith("programs"):
            continue
        match = ANCHOR_TOML_PROGRAM_RE.match(stripped)
        if match:
            name = match.group(1) or match.group(2)
            found.append({
                "file": rel, "line": lineno, "kind": "anchor_toml",
                "programId": match.group(2), "program": match.group(1),
            })
    return found, None


def idl_address(obj):
    """Extract the program address from an Anchor IDL object (any version)."""
    if not isinstance(obj, dict):
        return None
    addr = obj.get("address")
    if isinstance(addr, str):
        return addr
    meta = obj.get("metadata")
    if isinstance(meta, dict) and isinstance(meta.get("address"), str):
        return meta["address"]
    return None


def scan_idl_json(path, rel):
    """Return declarations from one Anchor IDL JSON file."""
    try:
        with open(path, encoding="utf-8", errors="replace") as fh:
            obj = json.load(fh)
    except (OSError, ValueError) as exc:
        return [], "unparsable IDL: %s" % exc
    addr = idl_address(obj)
    if not addr:
        return [], None
    return [{
        "file": rel, "line": 1, "kind": "idl_address", "programId": addr,
    }], None


def _is_idl_path(parts):
    return "idl" in parts


def crawl(root):
    """Walk the repository and collect declarations + scan errors."""
    declarations = []
    unresolvable = []
    scanned = {"rustFiles": 0, "anchorToml": 0, "idlJson": 0}
    root = os.path.abspath(root)
    for dirpath, dirnames, filenames in os.walk(root):
        dirnames[:] = sorted(
            d for d in dirnames if d not in SKIP_DIRS and not d.startswith(".")
        )
        for name in sorted(filenames):
            rel = os.path.relpath(os.path.join(dirpath, name), root)
            parts = rel.split(os.sep)
            entries, err = [], None
            if name.endswith(".rs"):
                scanned["rustFiles"] += 1
                entries, err = scan_rust_file(os.path.join(dirpath, name), rel)
            elif name == "Anchor.toml":
                scanned["anchorToml"] += 1
                entries, err = scan_anchor_toml(os.path.join(dirpath, name), rel)
            elif name.endswith(".json") and _is_idl_path(parts):
                scanned["idlJson"] += 1
                entries, err = scan_idl_json(os.path.join(dirpath, name), rel)
            declarations.extend(entries)
            if err:
                unresolvable.append({"file": rel, "reason": err})
    return declarations, scanned, unresolvable


def rpc_get_account_info(rpc_url, program_ids, timeout=20):
    """Batch getAccountInfo; returns ({programId: {exists, executable}}, error)."""
    payload = None  # built below; kept for readability of the batch split
    # Batch form for several ids.
    requests = [
        {"jsonrpc": "2.0", "id": i + 1, "method": "getAccountInfo",
         "params": [pid, {"encoding": "base64"}]}
        for i, pid in enumerate(program_ids)
    ]
    body = json.dumps(requests if len(program_ids) > 1 else requests[0]).encode()
    req = urllib.request.Request(
        rpc_url, data=body, headers={"Content-Type": "application/json"})
    try:
        with urllib.request.urlopen(req, timeout=timeout) as resp:
            data = json.loads(resp.read().decode())
    except (urllib.error.URLError, OSError, ValueError) as exc:
        return None, "rpc unreachable: %s" % exc
    replies = data if isinstance(data, list) else [data]
    out = {}
    by_id = {}
    for reply in replies:
        if not isinstance(reply, dict):
            continue
        by_id[reply.get("id")] = reply
    for i, pid in enumerate(program_ids):
        reply = by_id.get(i + 1) or by_id.get(1)
        if reply is None or "error" in reply:
            out[pid] = {"exists": None, "executable": None,
                        "error": str((reply or {}).get("error", "missing reply"))}
            continue
        value = (reply.get("result") or {}).get("value")
        if value is None:
            out[pid] = {"exists": False, "executable": False}
        else:
            out[pid] = {"exists": True,
                        "executable": bool(value.get("executable"))}
    return out, None


def find_rot(declarations, onchain):
    """Build findings: drift + on-chain rot (never unresolvable)."""
    findings = []
    declared_ids = {d["programId"] for d in declarations
                    if d["kind"] in ("declare_id", "program_id_const")}
    for d in declarations:
        if not is_valid_program_id(d["programId"]):
            findings.append({
                **d, "severity": "rot", "kind": "invalid-program-id",
                "message": "declared program ID is not valid base58/32-byte",
            })
            continue
        if d["kind"] in ("idl_address", "anchor_toml") and declared_ids \
                and d["programId"] not in declared_ids:
            findings.append({
                **d, "severity": "rot", "kind": "declared-id-drift",
                "message": ("%s declares %s but no declare_id! in this "
                            "repository declares it" % (d["kind"],
                                                        d["programId"])),
            })
    for pid, state in sorted(onchain.items()):
        if state.get("exists") is None:
            continue  # unresolvable, handled by caller
        cluster = state.get("cluster", "cluster")
        if not state["exists"]:
            findings.append({
                "severity": "rot", "kind": "program-not-found",
                "programId": pid, "cluster": state.get("cluster"),
                "message": "no account exists for this program ID on %s" % cluster,
            })
        elif not state["executable"]:
            findings.append({
                "severity": "rot", "kind": "not-executable",
                "programId": pid, "cluster": state.get("cluster"),
                "message": "account exists on %s but is not an executable "
                           "program" % cluster,
            })
    return findings


def build_report(root, declarations, scanned, file_unresolvable,
                 onchain, rpc_error, cluster):
    findings = find_rot(declarations, onchain)
    unresolvable = list(file_unresolvable)
    if rpc_error:
        unresolvable.append({"programId": "*", "reason": rpc_error})
    for pid, state in sorted(onchain.items()):
        if state.get("exists") is None:
            unresolvable.append({
                "programId": pid,
                "reason": state.get("error", "unknown rpc error"),
            })
    return {
        "root": os.path.abspath(root),
        "cluster": cluster,
        "scanned": scanned,
        "declarations": declarations,
        "onChain": [{"programId": pid, **state}
                    for pid, state in sorted(onchain.items())],
        "findings": findings,
        "rotTotal": len(findings),
        "unresolvable": unresolvable,
        "unresolvableTotal": len(unresolvable),
    }


def print_summary(report):
    print("## idl-freshness")
    print("")
    print("| metric | value |")
    print("| --- | --- |")
    print("| cluster | %s |" % report["cluster"])
    for k in ("rustFiles", "anchorToml", "idlJson"):
        print("| %s scanned | %s |" % (k, report["scanned"][k]))
    print("| declarations | %s |" % len(report["declarations"]))
    print("| on-chain checked | %s |" % len(report["onChain"]))
    print("| rot | %s |" % report["rotTotal"])
    print("| unresolvable | %s |" % report["unresolvableTotal"])
    if report["findings"]:
        print("")
        print("**Rot findings:**")
        for f in report["findings"]:
            loc = f.get("file") or f.get("programId", "?")
            if f.get("line"):
                loc = "%s:%s" % (loc, f["line"])
            print("- `%s` — %s (%s)" % (f["kind"], f["message"], loc))
    if report["unresolvable"]:
        print("")
        print("**Unresolvable (never counted as rot):**")
        for u in report["unresolvable"]:
            print("- %s — %s" % (u.get("programId") or u.get("file"),
                                 u["reason"]))


def main(argv=None):
    parser = argparse.ArgumentParser(description="Solana program/IDL freshness")
    parser.add_argument("root", nargs="?", default=".")
    parser.add_argument("--report", default="idl-freshness-report.json")
    parser.add_argument("--cluster", default="mainnet",
                        help="'mainnet', 'devnet', or a full JSON-RPC URL")
    parser.add_argument("--offline", action="store_true",
                        help="skip the on-chain check entirely")
    parser.add_argument("--fail-on-rot", action="store_true")
    args = parser.parse_args(argv)

    if not os.path.isdir(args.root):
        print("idl-freshness: not a directory: %s" % args.root,
              file=sys.stderr)
        return 2

    cluster = args.cluster
    if cluster == "mainnet":
        rpc_url, cluster_name = MAINNET_RPC, "mainnet-beta"
    elif cluster == "devnet":
        rpc_url, cluster_name = DEVNET_RPC, "devnet"
    else:
        rpc_url, cluster_name = args.cluster, args.cluster

    declarations, scanned, file_unresolvable = crawl(args.root)

    onchain, rpc_error = {}, None
    if not args.offline:
        # Cluster per declaration: cfg-attribute wins, otherwise default.
        by_cluster = {"mainnet": set(), "devnet": set(), None: set()}
        for d in declarations:
            if is_valid_program_id(d["programId"]):
                by_cluster.setdefault(d.get("cluster"), set()).add(d["programId"])
        # Anything pinned to one cluster checks against that cluster's RPC.
        # The rest fall back to --cluster (user-supplied default).
        routed = {"mainnet": MAINNET_RPC, "devnet": DEVNET_RPC}
        clusters_to_check = []
        for cluster_key, ids in by_cluster.items():
            if not ids:
                continue
            if cluster_key in routed:
                clusters_to_check.append((cluster_key, routed[cluster_key],
                                          sorted(ids)))
            else:
                target = cluster_name
                clusters_to_check.append((target, rpc_url, sorted(ids)))
        for cluster_label, url, ids in clusters_to_check:
            data, err = rpc_get_account_info(url, ids)
            if data is not None:
                for pid, state in data.items():
                    state = dict(state, cluster=cluster_label)
                    onchain[pid] = state
            else:
                rpc_error = (rpc_error or "")
                rpc_error += "%s[%s]: %s; " % (cluster_label, url, err)

    report = build_report(args.root, declarations, scanned,
                          file_unresolvable, onchain, rpc_error, cluster_name)
    with open(args.report, "w", encoding="utf-8") as fh:
        json.dump(report, fh, indent=2, sort_keys=True)
        fh.write("\n")
    print_summary(report)
    if args.fail_on_rot and report["rotTotal"]:
        return 3
    return 0


if __name__ == "__main__":
    sys.exit(main())
