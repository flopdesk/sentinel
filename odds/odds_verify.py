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
import re
import sys

HERE = os.path.dirname(os.path.abspath(__file__))
ARCHIVE = [os.path.join(HERE, "..", "explorer", "archive", "messages.jsonl"),
           os.path.join(HERE, "..", "..", "explorer", "archive", "messages.jsonl")]
DOC = next((p for p in (os.path.join(HERE, "public", "odds.json"),
                        os.path.join(HERE, "odds.json"),
                        os.path.join(HERE, "..", "public", "odds.json")) if os.path.exists(p)),
           os.path.join(HERE, "public", "odds.json"))
DIG_RE = re.compile(r"DYDX DIGEST (\d{4}-\d{2}-\d{2}) (\d{2}:\d{2}) UTC")


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


def _seg(text, name):
    for part in text.split(" | "):
        part = part.strip()
        if part.startswith(name + " ") or part == name:
            return ASSET_RE.findall(part)
    return []


ASSET_RE = re.compile(r"\b([A-Z0-9]{1,12}-USD)\b")


def _digests(frames):
    """Independent re-parse of the digest tape from the archive (no builder import)."""
    out = {}
    for seq in sorted(frames):
        f = frames[seq]
        if f.get("room") != "hermes-desk":
            continue
        text = f.get("text") or ""
        if not text.startswith("DYDX DIGEST"):
            continue
        m = DIG_RE.search(text)
        if not m:
            continue
        out[m.group(1)] = {"date": m.group(1), "seq": seq, "text": text,
                           "gain": _seg(text, "GAIN"), "loss": _seg(text, "LOSS")}
    return [out[d] for d in sorted(out)]


def check_families(doc, frames, problems):
    """Check 5: rotation and outcome re-derived from the raw frames.

    This re-implements the family semantics from the frame text only, and does not
    import the builder, so agreement is evidence rather than restatement.
    """
    import datetime as _dt
    digests = _digests(frames)
    if not digests:
        problems.append("no digest frames re-parsed from the archive")
        return 0
    by_date = {d["date"]: d for d in digests}
    seq_date = {d["seq"]: d["date"] for d in digests}
    dates = [d["date"] for d in digests]
    n_checked = 0
    for m in doc.get("markets", []):
        ev = m.get("evidence", {})
        ref_seq = ev.get("reference_seq")
        ref = by_date.get(seq_date.get(ref_seq)) if ref_seq is not None else None
        if ref is None:
            problems.append("%s: reference_seq %s is not a digest frame" % (m["id"], ref_seq))
            continue
        if ref["date"] != m.get("opened_from_date"):
            problems.append("%s: opened_from_date %s != frame date %s" % (m["id"], m.get("opened_from_date"), ref["date"]))
        y, mo, dd = (int(x) for x in ref["date"].split("-"))
        fam = ("gainrepeat", "lossmatch")[_dt.date(y, mo, dd).toordinal() % 2]
        if m.get("family") != fam:
            problems.append("%s: family %s != rotation key %s for %s" % (m["id"], m.get("family"), fam, ref["date"]))
        if not str(m.get("id", "")).endswith(fam):
            problems.append("%s: market id does not carry its rotated family %s" % (m["id"], fam))
        i = dates.index(ref["date"])
        nxt = digests[i + 1] if i + 1 < len(digests) else None
        st = m.get("resolution", {}).get("status")
        if nxt is None:
            if st == "RESOLVED":
                problems.append("%s: RESOLVED but no later digest exists" % m["id"])
            continue
        if st != "RESOLVED":
            continue
        n_checked += 1
        if ev.get("resolving_seq") != nxt["seq"]:
            problems.append("%s: resolving_seq %s is not the next digest by date (%s)"
                            % (m["id"], ev.get("resolving_seq"), nxt["seq"]))
        if fam == "gainrepeat":
            want = "YES" if (ref["gain"] and nxt["gain"] and set(ref["gain"]) & set(nxt["gain"])) else "NO"
            if not ref["gain"] or not nxt["gain"]:
                want = "VOID"
        else:
            if not ref["loss"] or not nxt["loss"]:
                want = "VOID"
            else:
                want = "YES" if ref["loss"][0] in nxt["loss"] else "NO"
        got = m.get("resolution", {}).get("outcome")
        if want == "VOID":
            problems.append("%s: content resolves VOID but market says %s" % (m["id"], got))
        elif got != want:
            problems.append("%s: outcome %s != re-derived %s (%s vs %s)"
                            % (m["id"], got, want, ref["loss"][:1], nxt["loss"]))
    return n_checked


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

    # 5: rotation + outcome re-derived from the raw frames
    fam_checked = check_families(doc, frames, problems)

    return {"markets": len(markets), "settled": len(settled), "called": len(called),
            "wins": len(wins), "convention": convention, "problems": problems,
            "outcomes_re_derived": fam_checked,
            "proofs_re_derived": matched.get(convention, 0) if convention else 0}


def selftest():
    import datetime as _dt

    def fam(date_str):
        y, mo, dd = (int(x) for x in date_str.split("-"))
        return ("gainrepeat", "lossmatch")[_dt.date(y, mo, dd).toordinal() % 2]

    d1, d2 = "2026-01-01", "2026-01-02"
    t1 = "DYDX DIGEST 2026-01-01 11:00 UTC | GAIN AAA-USD +1.0% | LOSS XXX-USD -1.0% | VOL ETH-USD $1M"
    t2 = "DYDX DIGEST 2026-01-02 11:00 UTC | GAIN BBB-USD +1.0% | LOSS YYY-USD -1.0% | VOL ETH-USD $1M"
    frames = {1: {"seq": 1, "room": "hermes-desk", "text": t1},
              2: {"seq": 2, "room": "hermes-desk", "text": t2}}
    h1, h2 = sha(t1), sha(t2)
    # this pair shares neither a GAIN asset nor the top LOSS asset, so the outcome is NO
    # under either family; fam() picks which family each date opens.

    def doc():
        return {"schema": "flop.odds.v2",
                "tape": {"last_seq": 2, "last_sha256": h2},
                "live_market": {"id": "mkt-%s-%s" % (d2, fam(d2)), "opened_from_date": d2, "family": fam(d2),
                                "evidence": {"reference_seq": 2, "reference_sha256": h2}},
                "accounting": {"settled": 1, "wins": 1, "losses": 0, "stake": 50, "pnl_paper_units": 50,
                               "uncalled_listed": 0, "hits_yes": 0, "base_rate_realised_yes": 0.0},
                "markets": [
                    {"id": "mkt-%s-%s" % (d1, fam(d1)), "family": fam(d1), "opened_from_date": d1,
                     "call": "NO", "called": True, "entry_price": 0.5,
                     "evidence": {"reference_seq": 1, "reference_sha256": h1,
                                  "resolving_seq": 2, "resolving_sha256": h2},
                     "resolution": {"status": "RESOLVED", "outcome": "NO"}},
                    {"id": "mkt-%s-%s" % (d2, fam(d2)), "family": fam(d2), "opened_from_date": d2,
                     "call": None, "called": False,
                     "evidence": {"reference_seq": 2, "reference_sha256": h2},
                     "resolution": {"status": "OPEN", "outcome": None}}]}
    checks = []
    r = verify(doc(), frames)
    checks.append(("clean document verifies", not r["problems"] and r["convention"] == "sha256(text)"
                   and r["outcomes_re_derived"] == 1)
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
    # new in v1: rotation and outcome must survive an independent re-derivation
    d = doc(); d["markets"][0]["family"] = fam(d2)
    checks.append(("wrong rotation family is caught",
                   any("rotation key" in p for p in verify(d, frames)["problems"])))
    d = doc(); d["markets"][0]["id"] = "mkt-%s-%s" % (d1, fam(d2))
    checks.append(("market id not carrying its family is caught",
                   any("does not carry its rotated family" in p for p in verify(d, frames)["problems"])))
    d = doc(); d["markets"][0]["opened_from_date"] = d2
    checks.append(("date/frame mismatch is caught",
                   any("opened_from_date" in p for p in verify(d, frames)["problems"])))
    bad = [n for n, ok in checks if not ok]
    for n, ok in checks:
        print("%-46s %s" % (n, "PASS" if ok else "FAIL"))
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
          "outcomes re-derived", res["outcomes_re_derived"],
          "convention", res["convention"])
    if res["problems"]:
        print("PROBLEMS", len(res["problems"]))
        for p in res["problems"][:20]:
            print(" -", p)
        sys.exit(1)
    print("ALL CHECKS PASS - published ledger re-derived from the tape clean")
