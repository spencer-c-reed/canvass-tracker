#!/usr/bin/env python3
"""Canvass production-campaign live status generator.

Reads REAL state from the (private) election-law repo + host and emits a
sanitized status.json into the public tracker repo. Counts, queue depth,
runner state, throughput, commit subjects only — no legal content, no secrets.

Resilient: every section is wrapped; one failure never blanks the file.
Run by cron every 10 min, then git commit + push (see push_status.sh).
"""
import json, os, re, subprocess, time, datetime, glob

ELAW = "/home/spencer/election-law"
TRACKER = "/home/spencer/canvass-tracker"
DB = os.path.join(ELAW, "db/election_law.db")
RUNNER_LOG = "/home/spencer/scripts/logs/canvass-runner.log"
CACHE = os.path.join(TRACKER, "tools/corpus_count.cache")

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

# ---- corpus doc count (cached; only refresh on success) ----
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
        return rec
    if cached:
        cached["stale"] = True
        return cached
    return {"documents": None, "as_of": None, "stale": True}

# ---- runner state ----
def runner():
    tail = sh(f"tail -30 {RUNNER_LOG}", timeout=10).splitlines()
    last_cycle = next((l for l in reversed(tail) if "runner start" in l or "runner end" in l), "")
    # idle-yield streak
    streak = 0
    for l in reversed(tail):
        if "maintenance holding DB lock" in l:
            streak += 1
        elif "runner start" in l or "runner end" in l or "Batch closed" in l or "launched" in l.lower():
            break
    alive = bool(sh("pgrep -f canvass-runner.sh", timeout=5))
    # current detached ingest from done-markers
    markers = glob.glob("/home/spencer/.canvass-ingest-done/*.running")
    current = os.path.basename(markers[0]).replace(".running", "") if markers else None
    # current ingest process
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
                or "freshness" in l.lower()]
    for l in campaign:
        try:
            d, s = l.split("|", 1)
            day = d.split()[0]
            by_day[day] = by_day.get(day, 0) + 1
        except Exception:
            pass
    # recent closes = commit subjects mentioning a close/ingest
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

def now_iso():
    return datetime.datetime.now().astimezone().strftime("%Y-%m-%dT%H:%M:%S%z")

def now_human():
    return sh('date +"%Y-%m-%d %I:%M %p %Z"', timeout=5) or now_iso()

def main():
    assessment = {}
    try:
        assessment = json.load(open(os.path.join(TRACKER, "assessment.json")))
    except Exception:
        pass
    status = {
        "generated_at": now_iso(),
        "generated_human": now_human(),
        "generated_unix": int(time.time()),
        "north_star": assessment.get("north_star", ""),
        "deadline": assessment.get("deadline"),
        "assessment_updated": assessment.get("assessment_updated"),
        "corpus": section(corpus, {"documents": None}),
        "runner": section(runner, {"alive": None}),
        "queue": section(queue, {}),
        "throughput": section(throughput, {}),
        "closure_criteria": assessment.get("closure_criteria", []),
        "gates": assessment.get("gates", []),
        "spencer_blockers": assessment.get("spencer_blockers", []),
    }
    # overall progress = mean of criteria pct
    pcts = [c.get("pct", 0) for c in status["closure_criteria"] if isinstance(c.get("pct"), (int, float))]
    status["overall_pct"] = round(sum(pcts) / len(pcts)) if pcts else None
    with open(os.path.join(TRACKER, "status.json"), "w") as fh:
        json.dump(status, fh, indent=2)
    print("wrote status.json", status.get("overall_pct"))

if __name__ == "__main__":
    main()
