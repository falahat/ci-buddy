import json
import math
import os
from pathlib import Path
import sqlite3
import statistics
import subprocess
import time
import xml.etree.ElementTree as ET


def db(folder):
    connection = sqlite3.connect(Path(folder) / "history.sqlite")
    connection.row_factory = sqlite3.Row
    connection.executescript('''PRAGMA journal_mode=WAL;
        CREATE TABLE IF NOT EXISTS runs(id INTEGER PRIMARY KEY, kind, key, tree, "commit", branch, submitter,
            slot, argv, queued_at, started_at, ended_at, exit, cpu_s, peak_bytes, reused_from);
        CREATE TABLE IF NOT EXISTS tests(id INTEGER PRIMARY KEY, name UNIQUE);
        CREATE TABLE IF NOT EXISTS results(test, run, status, ms, excerpt,
            PRIMARY KEY(test, run)) WITHOUT ROWID;
        CREATE INDEX IF NOT EXISTS runs_key ON runs(key);
        CREATE INDEX IF NOT EXISTS runs_tree ON runs(tree);
        CREATE INDEX IF NOT EXISTS runs_commit ON runs("commit");
        CREATE INDEX IF NOT EXISTS runs_branch ON runs(branch);
        CREATE INDEX IF NOT EXISTS results_run ON results(run);''')
    return connection


def record(folder, out, read_file):
    out = Path(out)
    job = json.loads(read_file(out / "job.json"))
    ingested = time.time()
    watermark = job.get("junit_ingested_at", job.get("started_at", job["queued_at"]))
    results = []
    for path in out.rglob("junit.xml"):
        if path.stat().st_mtime < watermark:
            continue
        for suite in ET.fromstring(read_file(path)).iter("testsuite"):
            for case in suite.findall("testcase"):
                failed = [node for node in case if node.tag in ("failure", "error")]
                status = "failed" if failed else "skipped" if case.find("skipped") is not None else "passed"
                seconds = float(case.attrib["time"])
                if not math.isfinite(seconds) or seconds < 0:
                    raise ValueError("invalid JUnit duration")
                text = "\n".join(node.get("message", "") + "\n" + "".join(node.itertext())
                    for node in [*failed, *case.findall("system-err"), *case.findall("system-out")]) if failed else None
                results.append((case.attrib["classname"] + "::" + case.attrib["name"], status,
                    round(seconds * 1000),
                    text.encode()[:4096].decode(errors="ignore") if text is not None else None))
    for prefix in ("", "bench_"):
        if prefix and prefix + "ended_at" not in job:
            continue
        started, ended = job.get(prefix + "started_at"), job.get(prefix + "ended_at")
        status = job.get(prefix + "exit")
        label = "bench" if prefix or job["kind"] == "bench" else "correctness"
        if started is not None and ended is not None:
            results.append(("phase/" + label, "passed" if status == 0 else "failed", (ended - started) * 1000,
                None if status == 0 else f"{label} exited {status}"))
        if prefix and started is not None:
            results.append(("phase/bench-wait", "passed", (started - job["bench_queued_at"]) * 1000, None))
        try:
            lines = read_file(out / (prefix.replace("_", "-") + "phases.tsv")).splitlines()
        except FileNotFoundError:
            lines = []
        for line in lines:
            name, seconds, status = line.split("\t")
            if not math.isfinite(float(seconds)) or float(seconds) < 0:
                raise ValueError("invalid phase duration")
            if Path(name).name != name:
                raise ValueError("phase name must be a filename")
            excerpt = None
            if int(status) != 0:
                try:
                    excerpt = read_file(out / (name + ".log")).encode()[-4096:].decode(errors="ignore")
                except FileNotFoundError:
                    excerpt = f"{name} exited {status}"
            results.append(("phase/" + name, "passed" if int(status) == 0 else "failed", float(seconds) * 1000, excerpt))
    values = {key: job.get(key) for key in ("kind", "key", "tree", "commit", "branch", "submitter",
        "queued_at", "started_at", "ended_at", "exit", "cpu_s", "peak_bytes", "reused_from")}
    if values["exit"] is None:
        values["exit"] = int(read_file(out / "exit"))
    if job.get("bench_required") and values["exit"] == 0:
        values["exit"] = job.get("bench_exit")
    values["ended_at"] = job.get("bench_ended_at", values["ended_at"])
    try:
        values["slot"] = int(read_file(out / "slot"))
    except FileNotFoundError:
        values["slot"] = None
    if "bench_ended_at" in job:
        for name, combine in (("cpu_s", sum), ("peak_bytes", max)):
            pair = [job.get(name), job.get("bench_" + name)]
            values[name] = combine(pair) if None not in pair else None
    values.update(id=int(out.name), argv=json.dumps(job["argv"]))
    connection = db(folder)
    with connection:
        columns = ",".join('"' + key + '"' for key in values)
        connection.execute(f"INSERT OR REPLACE INTO runs({columns}) VALUES({','.join('?' for _ in values)})", list(values.values()))
        for name, status, ms, excerpt in results:
            connection.execute("INSERT OR IGNORE INTO tests(name) VALUES(?)", (name,))
            connection.execute("INSERT OR REPLACE INTO results VALUES((SELECT id FROM tests WHERE name=?),?,?,?,?)",
                (name, int(out.name), status, ms, excerpt))
    connection.close()
    return ingested


def query(folder, argv):
    started = time.perf_counter()
    command, *args = argv
    allowed = {"test": (1,), "failures": (2,), "slowest": (0, 2), "change": (1, 2), "flaky": (0,), "gates": (0, 2)}
    if command not in allowed or len(args) not in allowed[command]:
        raise ValueError("expected a documented history question and its arguments")
    connection = db(folder)

    class Median(list):
        step = list.append
        def finalize(self):
            values = [value for value in self if value is not None]
            return statistics.median(values) if values else None

    connection.create_aggregate("median", 1, Median)
    join = ' FROM results s JOIN tests t ON t.id=s.test JOIN runs r ON r.id=s.run '
    where, parameters = "r.reused_from IS NULL", []
    if command == "test":
        where += " AND t.name=?"
        parameters = args
    elif command == "failures":
        if args[0] == "--branch":
            where += " AND r.branch=?"
            parameters = [args[1]]
        elif args[0] == "--range" and ".." in args[1]:
            parameters = subprocess.check_output(["git", "-C", str(Path(folder) / "git"), "rev-list", args[1]], text=True).splitlines()
            where += ' AND r."commit" IN (' + ",".join("?" for _ in parameters) + ")"
        else:
            raise ValueError("failures requires --branch or --range")
    elif command in ("slowest", "gates"):
        option = "--since" if command == "slowest" else "--days"
        if args and (args[0] != option or command == "slowest" and not args[1].endswith("d")):
            raise ValueError("expected " + option + " and a day count")
        where += " AND r.ended_at>=? AND " + ("r.exit=0 AND t.name NOT LIKE 'phase/%'" if command == "slowest" else "r.kind='gate' AND r.exit IS NOT NULL")
        parameters = [time.time() - int(args[1].removesuffix("d") if args else 7) * 86400]
    elif command == "change":
        commit = args[0] if len(args) == 2 else subprocess.check_output(["git", "-C", str(Path(folder) / "git"), "rev-parse", args[0]], text=True).strip()
        tree = args[1] if len(args) == 2 else subprocess.check_output(["git", "-C", str(Path(folder) / "git"), "rev-parse", commit + "^{tree}"], text=True).strip()
        where, parameters = '(r.tree=? OR r."commit"=?)', [tree, commit]
    def select(sql):
        return [dict(row) for row in connection.execute(sql, parameters)]
    if command == "test":
        rows = select('SELECT r.id AS run,r."commit",r.branch,r.ended_at,s.status,s.ms,s.excerpt' + join + ' WHERE ' + where + ' ORDER BY r.ended_at,r.id')
        failures = [row for row in rows if row["status"] == "failed"]
        durations = sorted(row["ms"] for row in rows if row["ms"] is not None and row["ended_at"] >= time.time() - 30 * 86400)
        answer = dict(name=args[0], runs=len(rows), failures=len(failures), failure_rate=len(failures) / len(rows) if rows else None,
            last_failure=failures[-1] if failures else None, p50_ms=statistics.median(durations) if durations else None,
            p95_ms=durations[math.ceil(len(durations) * .95) - 1] if durations else None)
    elif command == "failures":
        groups = {}
        for row in select('SELECT t.name,r."commit"' + join + " WHERE " + where + " AND s.status='failed' ORDER BY r.ended_at,r.id"):
            groups.setdefault(row["name"], []).append(row["commit"])
        answer = sorted([dict(name=name, failures=len(commits), first_commit=commits[0], last_commit=commits[-1])
            for name, commits in groups.items()], key=lambda item: (-item["failures"], item["name"]))
    elif command == "slowest":
        answer = dict(tests=select('SELECT t.name,median(s.ms) AS median_ms' + join + ' WHERE ' + where + ' GROUP BY t.id ORDER BY median_ms DESC,t.name'),
            suites=select('SELECT r.id AS run,r."commit",r.ended_at,CASE WHEN COUNT(s.ms)=COUNT(*) THEN SUM(s.ms) END AS total_ms' + join + ' WHERE ' + where + ' GROUP BY r.id ORDER BY r.ended_at,r.id'))
    elif command == "change":
        failures = select('SELECT s.run,t.name,s.excerpt' + join + ' WHERE ' + where + " AND s.status='failed'")
        answer = [dict(row) | {"failures": [item for item in failures if item["run"] == row["id"]]}
            for row in connection.execute('SELECT * FROM runs r WHERE ' + where + ' ORDER BY ended_at,id', parameters)]
    elif command == "flaky":
        answer = select('SELECT t.name,r.tree,COUNT(*) AS runs' + join + " WHERE " + where + " AND t.name NOT LIKE 'phase/%' AND s.status IN ('passed','failed') GROUP BY t.id,r.tree HAVING COUNT(DISTINCT s.status)=2 ORDER BY t.name,r.tree")
    else:
        answer = select('''SELECT strftime('%Y-%m-%d',ended_at,'unixepoch') AS day,COUNT(*) AS gates,
            MIN(started_at) AS first_started_at,MAX(ended_at) AS last_ended_at,
            median(correctness+bench) AS total_s,MAX(correctness+bench) AS max_total_s,
            median(started_at-queued_at) AS queue_s,median(correctness) AS correctness_s,
            median(bench_wait) AS bench_wait_s,median(bench) AS bench_s
            FROM (SELECT r.id,r.ended_at,r.started_at,r.queued_at,
            MAX(CASE WHEN t.name='phase/correctness' THEN s.ms/1000 END) AS correctness,
            MAX(CASE WHEN t.name='phase/bench-wait' THEN s.ms/1000 END) AS bench_wait,
            MAX(CASE WHEN t.name='phase/bench' THEN s.ms/1000 END) AS bench
            FROM runs r LEFT JOIN results s ON r.id=s.run LEFT JOIN tests t ON t.id=s.test WHERE ''' + where + ' GROUP BY r.id) GROUP BY day ORDER BY day')
    connection.close()
    print(json.dumps(dict(answer=answer, query_s=time.perf_counter() - started, host_load=os.getloadavg())))
