#!/usr/bin/env python3
"""Phase 4 acceptance test - independent verification of the published Agent Odds ledger.

Verifies public/odds.json against the raw archive tape (builds/explorer/archive/messages.jsonl)
through a code path that does not import odds_build.py:

  1. settlement provenance: every reference_sha256 / resolving_sha256 is re-derived from the
     archived frame at that seq. The hash convention itself is inferred from the data (the rig
     tries a list of candidate conventions and reports which one reproduces every published
     proof), so a match is evidence, not a restatement of the builder.
  2. no-lookahead: resolving_seq > reference_seq for every market, and the live market's
     reference hash equals the tape's own last_sha256.
  3. ledger arithmetic: settled == wins + losses (+ uncalled), pnl == stake * (wins - losses),
     base_rate_realised_yes == hits_yes / called_settled, all re-derived from the market list.
  4. OPEN vs evidence: a market marked OPEN must have no resolving_sha256.

Selftest tampers with the tape and the document and requires each check to fail.

Usage: python3 odds_verify.py [--selftest]
"""
import hashlib
import json
import os
import sys

HERE = os.path.dirname(os.path.abspath(__file__))
ARCHIVE = [os.path.join(HERE, "..", "explorer", "archive", "messages.jsonl"),
           os.path.join(HERE, "..", "..", "explorer", "archive", "messages.jsonl")]
DOC = os.path.join(HERE, "public", "odds.json")


def sha(b):
    if isinstance(b, str):
        b = b.encode()
    return hashlib.sha256(b).hexdigest()


def conventions(frame):
    """Candidate hash conventions, in the order the rig reports them."""
    text = frame.get("text", "")
    room = frame.get("room", "hermes-desk")
    seq = frame.get("seq")
    out = {
        "sha256(text)": sha(text),
        "sha256(room|seq|text)": sha("%s|%s|%s" % (room, seq, text)),
        "sha256(canonical_json(frame))": sha(json.dumps(frame, sort_keys=True)),
        "sha256(compact_json(frame))": sha(json.dumps(frame, sort_keys=True, separators=(",", ":"))),
        "sha256(json_text_only)": sha(json.dumps(text)),
    }
    return out


def load_archive(paths=None):
    for p in (paths or ARCHIVE):
        if os.path.exists(p):
            frames = {}
            with open(p) as fh:
                for line in fh:
                    line = line.strip()
                    if not line:
                        continue
                    try:
                        f = json.loads(line)
                    except Exception:
                        continue
                    if "seq" in f:
                        frames.setdefault(int(f["seq"]), f)
            if frames:
                return p, frames
    raise SystemExit("archive not found in %s" % (paths or ARCHIVE))


def verify(doc, frames, expect_convention=None):
    problems = []
    markets = doc.get("markets", [])
    # 1 + 2: provenance and no-lookahead
    matched = {}
    for m in markets:
        ev = m.get("evidence", {})
        for key, which in (("reference_seq", "reference_sha256"), ("resolving_seq", "resolving_sha256")):
            seq = ev.get(key)
            want = ev.get(which)
            if seq is None:
                if want is None:
                    continue
                problems.append("%s: %s present but %s missing" % (m["id"], which, key))
                continue
            frame = frames.get(int(seq))
            if frame is None:
                problems.append("%s: %s=%s not in archive" % (m["id"], key, seq))
                continue
            if want is None:
                if m.get("resolution", {}).get("status") == "OPEN":
                    pass  # OPEN markets legitimately carry no resolving proof
                else:
                    problems.append("%s: %s present but %s missing" % (m["id"], key, which))
                continue
            cands = conventions(frame)
            hits = [k for k, v in cands.items() if v == want]
            if not hits:
                problems.append("%s: no convention reproduces %s at seq %s" % (m["id"], which, seq))
            for h in hits:
                matched[h] = matched.get(h, 0) + 1
        if ev.get("resolving_seq") is not None and ev.get("reference_seq") is not None:
            if int(ev["resolving_seq"]) <= int(ev["reference_seq"]):
                problems.append("%s: resolving_seq <= reference_seq (lookahead)" % m["id"])
    convention = None
    if matched:
        convention = sorted(matched.items(), key=lambda kv: -kv[1])[0][0]
        total_proofs = 2 * len([m for m in markets if m.get("evidence", {}).get("resolving_sha256")])
        if matched[convention] < total_proofs:
            problems.append("convention %s explains only %d of %d proofs"
                            % (convention, matched[convention], total_proofs))
        if expect_convention and convention != expect_convention:
            problems.append("hash convention changed: %s (was %s)" % (convention, expect_convention))
    else:
        problems.append("no published proof could be re-derived from the archive")

    # live market reference hash must equal the tape's own last hash
    tape = doc.get("tape", {})
    live = doc.get("live_market") or {}
    lref = (live.get("evidence") or {}).get("reference_sha256")
    if tape.get("last_sha256") and lref and tape["last_sha256"] != lref:
        problems.append("live market reference_sha256 != tape.last_sha256")
    if tape.get("last_seq") and (live.get("evidence") or {}).get("reference_seq") != tape.get("last_seq"):
        problems.append("live market reference_seq != tape.last_seq")

    # 3: ledger arithmetic, re-derived from the markets list
    settled = [m for m in markets if m.get("resolution", {}).get("status") in ("RESOLVED", "VOID")]
    called = [m for m in settled if m.get("called")]
    wins = [m for m in called if m.get("resolution", {}).get("outcome") == "YES" and m.get("call") == "YES"] \
        + [m for m in called if m.get("resolution", {}).get("outcome") == "NO" and m.get("call") == "NO"]
    a = doc.get("accounting", {})
    if a.get("settled") != len(called):
        problems.append("accounting.settled %s != re-derived called-and-settled %d" % (a.get("settled"), len(called)))
    if a.get("uncalled_listed") is not None and a["uncalled_listed"] != len(settled) - len(called):
        problems.append("accounting.uncalled_listed %s != re-derived %d"
                        % (a.get("uncalled_listed"), len(settled) - len(called)))
    open_n = len([m for m in markets if m.get("resolution", {}).get("status") == "OPEN"])
    if len(markets) != len(called) + (len(settled) - len(called)) + open_n:
        problems.append("market totals do not reconcile: %d != %d" % (len(markets), len(settled) + open_n))
    if a.get("wins") != len(wins):
        problems.append("accounting.wins %s != re-derived %d" % (a.get("wins"), len(wins)))
    if len(wins) + a.get("losses", 0) != len(called):
        problems.append("wins + losses != called markets (%d + %s != %d)"
                        % (len(wins), a.get("losses"), len(called)))
    stake = a.get("stake")
    if stake is not None and a.get("pnl_paper_units") is not None:
        want = stake * (len(wins) - a.get("losses", 0))
        if abs(want - a["pnl_paper_units"]) > 1e-6:
            problems.append("pnl %s != stake*(wins-losses) %s" % (a["pnl_paper_units"], want))
    if called:
        yes = len([m for m in called if m.get("resolution", {}).get("outcome") == "YES"])
        if a.get("hits_yes") is not None and a["hits_yes"] != yes:
            problems.append("accounting.hits_yes %s != re-derived %d" % (a.get("hits_yes"), yes))
        if a.get("base_rate_realised_yes") is not None and abs(a["base_rate_realised_yes"] - yes / len(called)) > 1e-6:
            problems.append("base_rate_realised_yes %s != %s" % (a.get("base_rate_realised_yes"), yes / len(called)))
    # 4: OPEN carries no resolving proof
    for m in markets:
        st = m.get("resolution", {}).get("status")
        if st == "OPEN" and m.get("evidence", {}).get("resolving_sha256"):
            problems.append("%s: OPEN market carries a resolving hash" % m["id"])

    return {"markets": len(markets), "settled": len(settled), "called": len(called),
            "wins": len(wins), "convention": convention, "problems": problems,
            "proofs_re_derived": matched.get(convention, 0) if convention else 0}


def selftest():
    frames = {1: {"seq": 1, "room": "hermes-desk", "text": "GAIN: a, b | PAIN: c"},
              2: {"seq": 2, "room": "hermes-desk", "text": "GAIN: a | PAIN: d"}}
    h2 = sha(frames[2]["text"])

    def doc():
        return {"schema": "flop.odds.v1",
                "tape": {"last_seq": 2, "last_sha256": h2},
                "live_market": {"id": "m2", "evidence": {"reference_seq": 2, "reference_sha256": h2}},
                "accounting": {"settled": 1, "wins": 1, "losses": 0, "stake": 50, "pnl_paper_units": 50,
                               "uncalled_listed": 0, "hits_yes": 0, "base_rate_realised_yes": 0.0},
                "markets": [
                    {"id": "m1", "call": "NO", "called": True, "entry_price": 0.5,
                     "evidence": {"reference_seq": 1, "reference_sha256": sha(frames[1]["text"]),
                                  "resolving_seq": 2, "resolving_sha256": h2},
                     "resolution": {"status": "RESOLVED", "outcome": "NO"}},
                    {"id": "m2", "call": None, "called": False,
                     "evidence": {"reference_seq": 2, "reference_sha256": h2},
                     "resolution": {"status": "OPEN", "outcome": None}}]}
    checks = []
    r = verify(doc(), frames)
    checks.append(("clean document verifies", not r["problems"] and r["convention"] == "sha256(text)")
                  or (print("  clean problems:", r["problems"]) or False))
    d = doc(); d["markets"][0]["evidence"]["resolving_sha256"] = sha("tampered")
    checks.append(("tampered proof is caught", any("no convention" in p for p in verify(d, frames)["problems"])))
    d = doc(); d["markets"][0]["resolution"]["outcome"] = "YES"
    checks.append(("flipped outcome breaks the ledger", bool(verify(d, frames)["problems"])))
    d = doc(); d["markets"][0]["evidence"]["resolving_seq"] = 1
    checks.append(("lookahead is caught", any("lookahead" in p for p in verify(d, frames)["problems"])))
    d = doc(); d["markets"][1]["evidence"]["resolving_sha256"] = sha("x")
    checks.append(("OPEN with a resolving hash is caught", bool(verify(d, frames)["problems"])))
    d = doc(); d["tape"]["last_sha256"] = sha("other")
    checks.append(("live-vs-tape mismatch is caught", bool(verify(d, frames)["problems"])))
    d = doc(); del d["markets"][0]["evidence"]["reference_seq"]
    checks.append(("missing seq is caught", bool(verify(d, frames)["problems"])))
    d = doc(); d["accounting"]["pnl_paper_units"] = 12.5
    checks.append(("pnl arithmetic is checked", bool(verify(d, frames)["problems"])))
    d = doc(); d["accounting"]["wins"] = 5
    checks.append(("win count is checked", bool(verify(d, frames)["problems"])))
    bad = [n for n, ok in checks if not ok]
    for n, ok in checks:
        print("%-40s %s" % (n, "PASS" if ok else "FAIL"))
    print("selftest %d/%d %s" % (len(checks) - len(bad), len(checks), "PASS" if not bad else "FAIL " + str(bad)))
    return not bad


if __name__ == "__main__":
    if "--selftest" in sys.argv:
        sys.exit(0 if selftest() else 1)
    path, frames = load_archive()
    doc = json.load(open(DOC))
    res = verify(doc, frames)
    print("archive", path, "frames", len(frames))
    print("markets", res["markets"], "settled", res["settled"], "called", res["called"],
          "wins", res["wins"], "proofs re-derived", res["proofs_re_derived"],
          "convention", res["convention"])
    if res["problems"]:
        print("PROBLEMS", len(res["problems"]))
        for p in res["problems"][:20]:
            print(" -", p)
        sys.exit(1)
    print("ALL CHECKS PASS - published ledger re-derived from the tape clean")
