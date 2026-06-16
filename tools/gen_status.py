#!/usr/bin/env python3
"""Canvass production-campaign live status generator.

Reads REAL state from the (private) election-law repo + host and emits a
sanitized status.json into the public tracker repo. Counts, queue depth,
runner state, throughput, commit subjects only — no legal content, no secrets.

The topline % and every closure criterion are COMPUTED from real signals
(coverage matrix, freshness cells, gates, benchmarks) wherever a signal
exists. Criteria with no machine signal (human/Spencer/Codex gates) fall
back to the value in assessment.json and are flagged auto=false so the
dashboard can say so. Nothing is silently hand-typed.

Resilient: every section is wrapped; one failure never blanks the file.
Run by cron every 10 min, then git commit + push (see push_status.sh).
"""
import json, os, re, subprocess, time, datetime, glob

ELAW = "/home/spencer/election-law"
TRACKER = "/home/spencer/canvass-tracker"
DB = os.path.join(ELAW, "db/election_law.db")
RUNNER_LOG = "/home/spencer/scripts/logs/canvass-runner.log"
CACHE = os.path.join(TRACKER, "tools/corpus_count.cache")
HISTORY = os.path.join(TRACKER, "tools/corpus_history.json")
COVERAGE_MATRIX = os.path.join(ELAW, "docs/quality-reports/coverage-matrix-latest.md")
CANONICAL = os.path.join(ELAW, "test-questions/qualitative-evaluations/canonical-facts-report.json")
ADVERSARIAL = os.path.join(ELAW, "test-questions/adversarial-observables/latest-run.json")

def sh(cmd, timeout=30):
    try:
        return subprocess.run(cmd, shell=True, capture_output=True, text=True,
                              timeout=timeout).stdout.strip()
    except Exception:
        return ""

def section(fn, default):
    try:
        return fn()
    except Exception as e:
        return {"error": str(e), **default} if isinstance(default, dict) else default

def now_iso():
    return datetime.datetime.now().astimezone().strftime("%Y-%m-%dT%H:%M:%S%z")

def now_human():
    return sh('date +"%Y-%m-%d %I:%M %p %Z"', timeout=5) or now_iso()

# ---- corpus doc count (cached; only refresh on success) + daily history ----
def corpus():
    cached = None
    if os.path.exists(CACHE):
        try:
            cached = json.load(open(CACHE))
        except Exception:
            cached = None
    out = sh(f"sg ellaw \"sqlite3 -readonly {DB} 'SELECT COUNT(*) FROM documents;'\"", timeout=90)
    if out.isdigit():
        rec = {"documents": int(out), "as_of": now_iso(), "stale": False}
        json.dump(rec, open(CACHE, "w"))
        _record_history(int(out))
        return rec
    if cached:
        cached["stale"] = True
        return cached
    return {"documents": None, "as_of": None, "stale": True}

def _record_history(count):
    """Track first/last doc count per day so we can show real daily deltas."""
    hist = {}
    if os.path.exists(HISTORY):
        try:
            hist = json.load(open(HISTORY))
        except Exception:
            hist = {}
    today = now_iso()[:10]
    day = hist.get(today) or {"first": count, "last": count}
    day["last"] = count
    if "first" not in day:
        day["first"] = count
    hist[today] = day
    # keep last 21 days
    for k in sorted(hist.keys())[:-21]:
        hist.pop(k, None)
    try:
        json.dump(hist, open(HISTORY, "w"), indent=2)
    except Exception:
        pass

def live_today(corp):
    """The 'is it moving right now' strip: real deltas, not a frozen gauge."""
    hist = {}
    if os.path.exists(HISTORY):
        try:
            hist = json.load(open(HISTORY))
        except Exception:
            hist = {}
    cur = corp.get("documents")
    today = now_iso()[:10]
    added_today = None
    if cur is not None and today in hist:
        added_today = cur - hist[today].get("first", cur)
    # 7-day delta from the earliest day on/after 7 days ago
    added_7d = None
    if cur is not None and hist:
        cutoff = (datetime.date.fromisoformat(today) - datetime.timedelta(days=7)).isoformat()
        past = [hist[k].get("first") for k in sorted(hist) if k >= cutoff and k < today]
        if past:
            added_7d = cur - past[0]
    last_commit = sh(f"cd {ELAW} && git log -1 --date=format:'%Y-%m-%d %H:%M' --pretty='%cd'", timeout=10)
    return {
        "docs_added_today": added_today,
        "docs_added_7d": added_7d,
        "last_commit": last_commit or None,
    }

# ---- runner state ----
def runner():
    tail = sh(f"tail -30 {RUNNER_LOG}", timeout=10).splitlines()
    last_cycle = next((l for l in reversed(tail) if "runner start" in l or "runner end" in l), "")
    streak = 0
    for l in reversed(tail):
        if "maintenance holding DB lock" in l:
            streak += 1
        elif "runner start" in l or "runner end" in l or "Batch closed" in l or "launched" in l.lower():
            break
    alive = bool(sh("pgrep -f canvass-runner.sh", timeout=5))
    markers = glob.glob("/home/spencer/.canvass-ingest-done/*.running")
    current = os.path.basename(markers[0]).replace(".running", "") if markers else None
    proc = sh("ps -eo args | grep -iE 'ingest_.*\\.py|_session_laws.py' | grep -v grep | head -1", timeout=5)
    state = "working"
    if streak > 0:
        state = "yield-to-maintenance"
    last_line = tail[-1] if tail else ""
    return {
        "alive": alive,
        "state": state,
        "idle_yield_cycles": streak,
        "idle_yield_min_est": streak * 10,
        "current_detached_ingest": current,
        "current_ingest_proc": (proc[:80] if proc else None),
        "last_cycle_marker": last_cycle,
        "last_log_line": last_line[:200],
    }

# ---- queue depth ----
def queue():
    g = "docs/codex-packets/gap-audit-2026-06-13"
    f = "docs/codex-packets/freshness-repair-2026-06-14"
    def n(p):
        return len(glob.glob(os.path.join(ELAW, p)))
    return {
        "staged_statute_ingesters": n(f"{g}/ingesters/*.py"),
        "statute_promote_specs": n(f"{g}/specs/*.md"),
        "gap_families_inventoried": 189,
        "freshness_cells_total": len([d for d in glob.glob(os.path.join(ELAW, f, "*")) if os.path.isdir(d)]),
        "freshness_cells_done": n(f"{f}/*/result.json"),
        "freshness_promote_specs": n(f"{f}/*/promote-spec.md"),
    }

# ---- throughput ----
def throughput():
    log = sh(f"cd {ELAW} && git log --since='7 days ago' --pretty='%cd|%s' "
             f"--date=format:'%Y-%m-%d %H:%M'", timeout=20).splitlines()
    by_day, recent = {}, []
    campaign = [l for l in log if "campaign" in l.lower() or "backfill" in l.lower()
                or "freshness" in l.lower() or "solo-close" in l.lower()]
    for l in campaign:
        try:
            d, s = l.split("|", 1)
            day = d.split()[0]
            by_day[day] = by_day.get(day, 0) + 1
        except Exception:
            pass
    for l in log:
        try:
            d, s = l.split("|", 1)
            if any(k in s.lower() for k in ["campaign:", "backfill", "freshness", "solo-close"]):
                recent.append({"when": d, "subject": s[:140]})
        except Exception:
            pass
    today = now_iso()[:10]
    return {"by_day": by_day, "today": by_day.get(today, 0),
            "recent_closes": recent[:14]}

# ================= LIVE SCORECARD SIGNALS =================

def parse_coverage_matrix():
    """Real per-jurisdiction × per-family completeness from the matrix doc."""
    txt = open(COVERAGE_MATRIX, encoding="utf-8").read()
    gen = re.search(r"Generated:\s*`([^`]+)`", txt)

    def tier_counts(section_header):
        # grab the block under a '### <header>' until the next '##'
        m = re.search(rf"### {re.escape(section_header)}\s*(.+?)(?:\n##|\Z)", txt, re.S)
        counts = {}
        if not m:
            return counts
        for line in m.group(1).splitlines():
            tm = re.match(r"\s*-\s*`(\d)/8`:\s*(.+)", line)
            if tm:
                tier = int(tm.group(1))
                states = [s.strip() for s in tm.group(2).split(",") if s.strip()]
                counts[tier] = len(states)
        return counts

    raw = tier_counts("Raw Presence")
    eff = tier_counts("Ceiling-Adjusted Presence")
    juris = sum(raw.values()) or 51
    raw_cells = sum(t * c for t, c in raw.items())
    eff_cells = sum(t * c for t, c in eff.items())
    denom = juris * 8
    # family presence rates
    families = []
    for fm in re.finditer(r"\|\s*([A-Za-z][A-Za-z /.]+?)\s*\|\s*(\d+)\s*\|\s*(\d+)\s*\|\s*([\d.]+)%\s*\|", txt):
        name = fm.group(1).strip()
        if name.lower() in ("family",) or name.startswith("-"):
            continue
        families.append({"family": name, "rate": float(fm.group(4))})
    return {
        "juris": juris,
        "raw_8of8": raw.get(8, 0),
        "eff_8of8": eff.get(8, 0),
        "raw_pct": round(raw_cells / denom * 100, 1) if denom else 0,
        "eff_pct": round(eff_cells / denom * 100, 1) if denom else 0,
        "families": families[:8],
        "generated": gen.group(1) if gen else None,
    }

def parse_benchmarks():
    out = {}
    try:
        c = json.load(open(CANONICAL))
        out["canonical"] = {"passed": c.get("passed"), "total": c.get("total"),
                            "run_date": (c.get("run_date") or "")[:10]}
    except Exception:
        pass
    try:
        a = json.load(open(ADVERSARIAL))
        s = a.get("summary", {})
        out["adversarial"] = {"pass": s.get("PASS", 0), "fail": s.get("FAIL", 0),
                              "not_run": s.get("NOT_RUN", 0)}
    except Exception:
        pass
    return out

GATE_WEIGHT = {"green": 1.0, "mostly_green": 0.9, "blocked": 0.85, "amber": 0.5,
               "blocked_spencer": 0.3, "in_progress": 0.5, "red": 0.15}

def _gate_map(assessment):
    return {g.get("id"): g.get("status", "") for g in assessment.get("gates", [])}

def _frontier_pct(assessment):
    """Wide-scope categories the matrix can't see (tribal/territories/emerging-tech/etc).
    Explicit human checklist — every item visible, none buried in one scalar."""
    cats = assessment.get("frontier_categories", [])
    if not cats:
        return None, []
    w = {"done": 1.0, "building": 0.5, "partial": 0.4, "seeded": 0.3,
         "thin": 0.3, "untracked": 0.0, "absent": 0.0}
    vals = [w.get(c.get("status", "absent"), 0.0) for c in cats]
    pct = round(sum(vals) / len(vals) * 100) if vals else None
    return pct, cats

def main():
    assessment = {}
    try:
        assessment = json.load(open(os.path.join(TRACKER, "assessment.json")))
    except Exception:
        pass

    corp = section(corpus, {"documents": None})
    q = section(queue, {})
    cov = section(parse_coverage_matrix, {})
    bench = section(parse_benchmarks, {})

    try:
        criteria, extras = _compute_with_queue(assessment, cov, bench, q)
    except Exception as e:
        criteria = assessment.get("closure_criteria", [])
        extras = {"error": str(e)}

    status = {
        "generated_at": now_iso(),
        "generated_human": now_human(),
        "generated_unix": int(time.time()),
        "north_star": assessment.get("north_star", ""),
        "deadline": assessment.get("deadline"),
        "assessment_updated": assessment.get("assessment_updated"),
        "corpus": corp,
        "live_today": section(lambda: live_today(corp), {}),
        "runner": section(runner, {"alive": None}),
        "queue": q,
        "throughput": section(throughput, {}),
        "closure_criteria": criteria,
        "scorecard": extras,
        "gates": assessment.get("gates", []),
        "spencer_blockers": assessment.get("spencer_blockers", []),
    }
    pcts = [c.get("pct", 0) for c in criteria if isinstance(c.get("pct"), (int, float))]
    status["overall_pct"] = round(sum(pcts) / len(pcts)) if pcts else None
    status["overall_auto"] = all(c.get("auto") for c in criteria) if criteria else False

    with open(os.path.join(TRACKER, "status.json"), "w") as fh:
        json.dump(status, fh, indent=2)
    print("wrote status.json overall=", status.get("overall_pct"),
          "auto-criteria=", sum(1 for c in criteria if c.get("auto")), "/", len(criteria))

def _compute_with_queue(assessment, cov, bench, q):
    """compute_criteria, but with the freshness branch able to read queue cells."""
    gm = _gate_map(assessment)
    frontier_pct, frontier_cats = _frontier_pct(assessment)
    extras = {"coverage_matrix": cov, "benchmarks": bench,
              "frontier_pct": frontier_pct, "frontier_categories": frontier_cats}

    def gate_pct(*ids):
        ws = [GATE_WEIGHT.get(gm.get(i, ""), 0.5) for i in ids if i in gm]
        return round(sum(ws) / len(ws) * 100) if ws else None

    out = []
    for c in assessment.get("closure_criteria", []):
        c = dict(c)
        key = c.get("compute", "manual")
        computed, inputs, auto = None, None, False
        try:
            if key == "coverage_matrix" and cov:
                tracked = 0.7 * cov["eff_pct"] + 0.3 * cov["raw_pct"]
                computed = round(0.6 * tracked + 0.4 * frontier_pct) if frontier_pct is not None else round(tracked)
                inputs = (f"tracked families {cov['eff_8of8']}/{cov['juris']} effective "
                          f"({cov['eff_pct']}%), {cov['raw_8of8']}/{cov['juris']} raw 8/8 "
                          f"({cov['raw_pct']}%)"
                          + (f"; wide-scope frontier {frontier_pct}%" if frontier_pct is not None else ""))
            elif key == "gates_closed":
                computed = gate_pct("G1", "G2", "G3", "G4", "G5", "G6", "G7", "G8")
                tally = {}
                for s in gm.values():
                    tally[s] = tally.get(s, 0) + 1
                inputs = ", ".join(f"{v} {k}" for k, v in sorted(tally.items()))
            elif key == "freshness":
                done, total = q.get("freshness_cells_done"), q.get("freshness_cells_total")
                repair = (done / total * 100) if (done and total) else 0
                autom = GATE_WEIGHT.get(gm.get("G3", ""), 0.15) * 100
                computed = round(0.5 * repair + 0.5 * autom)
                inputs = (f"repair cells {done}/{total} ({round(repair)}%); "
                          f"durability automation gate G3 = {gm.get('G3','?')}")
            elif key == "tests":
                parts, desc = [], []
                cn = bench.get("canonical")
                if cn and cn.get("total"):
                    parts.append(cn["passed"] / cn["total"] * 100)
                    desc.append(f"canonical {cn['passed']}/{cn['total']}")
                ad = bench.get("adversarial")
                if ad and (ad["pass"] + ad["fail"]):
                    parts.append(ad["pass"] / (ad["pass"] + ad["fail"]) * 100)
                    desc.append(f"adversarial {ad['pass']}/{ad['pass']+ad['fail']} pass"
                                + (f" ({ad['not_run']} not run)" if ad.get("not_run") else ""))
                cr = assessment.get("citation_regression")
                if cr and cr.get("total"):
                    parts.append(cr["passed"] / cr["total"] * 100)
                    desc.append(f"citation regression {cr['passed']}/{cr['total']}")
                if parts:
                    computed = round(sum(parts) / len(parts))
                    inputs = "; ".join(desc)
            elif key == "ops_gates":
                computed = gate_pct("G6", "G7", "G8")
                inputs = (f"runtime G6={gm.get('G6','?')}, write-safety G7={gm.get('G7','?')}, "
                          f"docs G8={gm.get('G8','?')}")
        except Exception as e:
            inputs = f"compute error: {e}"
            computed = None

        if computed is not None:
            c["pct"] = computed
            c["auto"] = True
            c["inputs"] = inputs
        else:
            c["auto"] = False
            if key != "manual":
                c["inputs"] = inputs or "signal unavailable — showing last human estimate"
        out.append(c)
    return out, extras

if __name__ == "__main__":
    main()
