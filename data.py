import fcntl
import hashlib
import json
import os
from pathlib import Path
import shlex
import shutil
import stat
import subprocess
import sys
import time
import tomllib
import history


def git(folder, *args):
    return subprocess.check_output(["git", "-C", str(folder), *args], text=True).strip()


def emit(values):
    for key, value in values.items():
        print(f"{key}={shlex.quote(str(value))}")


def open_file(path, mode="r"):
    flags = {"r": os.O_RDONLY, "w": os.O_WRONLY | os.O_CREAT | os.O_TRUNC,
             "a": os.O_WRONLY | os.O_CREAT | os.O_APPEND}[mode]
    descriptor = os.open(path, flags | os.O_NOFOLLOW | os.O_NONBLOCK, 0o600)
    if not stat.S_ISREG(os.fstat(descriptor).st_mode):
        os.close(descriptor)
        raise ValueError(f"not a regular file: {path}")
    return os.fdopen(descriptor, mode, encoding="utf-8")


def read_file(path):
    with open_file(path) as stream:
        return stream.read()


def write_file(path, content):
    temporary = path.with_suffix(".tmp")
    with open_file(temporary, "w") as stream:
        stream.write(content)
    temporary.replace(path)


def project_config(repository, commit="HEAD"):
    config = tomllib.loads(git(repository, "show", commit + ":ci-buddy.toml"))
    allowed = {"name", "host", "main", "image", "prepare", "wrapper", "gate", "test", "bench"}
    if config.keys() - allowed:
        raise ValueError(f"unknown project settings: {config.keys() - allowed}")
    for name in ("test", "gate", "bench"):
        if name == "test" or name in config:
            if not isinstance(config[name], str) or not config[name].strip():
                raise ValueError(f"{name} requires a nonempty command")
    return config


command, *args = sys.argv[1:]
if command == 'available':
    root = Path(args[0])
    limits = tomllib.loads((root / 'host.conf').read_text())
    if shutil.disk_usage(root).free < limits['min_free_gb'] * 10**9:
        raise SystemExit('free disk below configured floor')
    subprocess.run([str(root / 'bin/pueue'), '--config', str(root / 'pueue.yml'), 'status', '--json'],
                   stdout=subprocess.DEVNULL, stderr=subprocess.DEVNULL, check=True)
    sys.exit()
if command in ("read", "field"):
    try:
        content = read_file(args[0])
    except FileNotFoundError:
        if command == "read":
            raise
        sys.exit()
    sys.stdout.write(str(int(content)) if command == "field" and content else content)
elif command == "write":
    write_file(Path(args[0]), args[1] + "\n")
elif command == "append":
    with open_file(args[0], "a") as stream:
        stream.write(args[1] + "\n")
elif command == "host":
    config = tomllib.loads(Path(args[0], "host.conf").read_text())
    allowed = {"jobs", "slots", "memory", "cpus", "min_free_gb", "slot_max_gb", "keep_days", "workers"}
    if config.keys() - allowed:
        raise ValueError(f"unknown host settings: {config.keys() - allowed}")
    config.setdefault("keep_days", 90)
    emit({key: config[key] for key in ("jobs", "slots", "memory", "cpus", "min_free_gb", "slot_max_gb", "keep_days")})
elif command == "job":
    folder, run, kind, commit, base, branch, submitter, *argv = args
    config = project_config(Path(folder, "git"), commit)
    dispatched = argv[:1] == ["--worker"]
    if dispatched:
        argv = argv[1:]
    rerun = kind != "run" and argv[:1] == ["--rerun"]
    if rerun:
        argv = argv[1:]
    if kind == "test" and argv[:1] == ["--since"]:
        base, argv = argv[1], argv[2:]
    if kind not in ("run", "test", "gate", "bench"):
        raise ValueError(f"unknown job kind: {kind}")
    exclusive = kind == "bench" or kind in ("run", "gate") and argv[:1] == ["--exclusive"]
    if kind in ("run", "gate") and exclusive:
        argv = argv[1:]
    if kind == "run" and argv[:1] == ["--"]:
        argv = argv[1:]
    if kind == "run" and not argv:
        raise ValueError("run requires a command")
    if kind in ("gate", "bench") and (kind not in config or argv):
        raise ValueError(f"{kind} requires its configured command and no arguments")
    job = dict(kind=kind, commit=commit, base=base, branch=branch, submitter=submitter,
               tree=git(Path(folder, "git"), "rev-parse", commit + "^{tree}"),
               argv=argv, key="", rerun=rerun, queued_at=time.time(), exclusive=exclusive,
               bench_required=kind == "gate" and "bench" in config,
               dispatched=dispatched)
    if kind != "run":
        revision = read_file(Path(folder).parents[1] / "bin/revision").strip()
        inputs = [kind, job["tree"], argv, base if kind == "test" and not argv else "", revision]
        job["key"] = hashlib.sha256(json.dumps(inputs, sort_keys=True).encode()).hexdigest()
    write_file(Path(folder, "runs", run, "job.json"), json.dumps(job))
    for name in ('task', 'slot', 'exit', 'bench-task', 'bench-exit', 'worker-exit'):
        write_file(Path(folder, 'runs', run, name), '')
    print("exclusive" if exclusive else "jobs")
elif command == "reuse":
    folder, run = Path(args[0]), args[1]
    out = folder / "runs" / run
    job = json.loads(read_file(out / "job.json"))
    tasks = json.load(sys.stdin)["tasks"]
    if job["key"] and not job["rerun"]:
        for task in tasks.values():
            label = task.get("label") or ""
            if "Done" not in task["status"] and label.startswith(folder.name + "/") and job["key"] in label.split("/"):
                original = label.split("/")[1]
                print("active", original, read_file(folder / "runs" / original / "task").strip())
                git(folder / "git", "update-ref", "-d", "refs/ci-buddy/" + run)
                shutil.rmtree(out)
                sys.exit()
        database = history.db(folder)
        for row in database.execute('SELECT * FROM runs WHERE key=? AND reused_from IS NULL AND exit IS NOT NULL ORDER BY id DESC', (job["key"],)):
            prior = dict(row)
            complete = database.execute("SELECT 1 FROM results JOIN tests ON tests.id=results.test WHERE run=? AND name='phase/bench'", (prior["id"],)).fetchone()
            if job["bench_required"] and prior["exit"] == 0 and not complete:
                continue
            original = prior["id"]
            prior.update(id=int(run), commit=job["commit"], branch=job["branch"], submitter=job["submitter"],
                         queued_at=job["queued_at"], started_at=time.time(), ended_at=time.time(), cpu_s=0, peak_bytes=None, reused_from=original)
            columns = ','.join('"' + key + '"' for key in prior)
            with database:
                database.execute(f'INSERT INTO runs ({columns}) VALUES ({",".join("?" for _ in prior)})', tuple(prior.values()))
                database.execute('INSERT INTO results SELECT test,?,status,ms,excerpt FROM results WHERE run=?', (int(run), original))
            job.update(reused_from=original, exit=prior["exit"])
            write_file(out / "job.json", json.dumps(job))
            write_file(out / "exit", str(prior["exit"]))
            if job["bench_required"]:
                write_file(out / "bench-exit", str(prior["exit"]))
            previous = folder / "runs" / str(original) / "report.txt"
            report = read_file(previous) if previous.exists() else json.dumps(prior)
            write_file(out / "report.txt", f"reused_from={original}\n{report}")
            print("finished", original)
            break
elif command == "history":
    history.query(Path(args[0]), args[1:])
elif command == "log":
    folder, run = Path(args[0]), args[1]
    output = folder / "runs" / run
    job = json.loads(read_file(output / "job.json"))
    output = folder / "runs" / str(job.get("reused_from", run))
    if any((folder / "logs").glob("*/" + output.name + ".tar.zst")):
        subprocess.run([sys.executable, str(folder.parents[1] / "bin/archive.py"), str(output), "log"], check=True)
    else:
        subprocess.run(["pueue", "log", "--full", read_file(output / "task").strip()], check=True)
elif command == "record":
    output = Path(args[0])
    path = output / "job.json"
    job = json.loads(read_file(path))
    job["junit_ingested_at"] = history.record(output.parents[1], output, read_file)
    write_file(path, json.dumps(job))
    print(f"history_bytes={(output.parents[1] / 'history.sqlite').stat().st_size}")
elif command == "affected":
    output, repository = map(Path, args)
    job = json.loads(read_file(output / "job.json"))
    metadata = json.loads(read_file(output / "metadata.json"))
    packages = [(Path(p["manifest_path"]).relative_to("/work/src").parent, p["name"]) for p in metadata["packages"]]
    changed = list(filter(None, git(repository, "diff", "--name-only", "--no-renames", "-z", job["base"], job["commit"]).split("\0")))
    touched, shared = set(), False
    for name in changed:
        owners = [(len(path.parts), package) for path, package in packages if Path(name).is_relative_to(path)]
        if not owners or name in ("Cargo.toml", "Cargo.lock", "rust-toolchain.toml") or name.split("/")[0] in (".cargo", ".config"):
            shared = True
        else:
            touched.add(max(owners)[1])
    expression = "all()" if shared or not touched else " + ".join("rdeps(=" + name + ")" for name in sorted(touched))
    job["touched_packages"], job["selection"] = sorted(touched), expression if not job["argv"] else "explicit"
    write_file(output / "job.json", json.dumps(job))
    emit(dict(affected=expression if not job["argv"] else ""))
elif command == "load":
    job = json.loads(read_file(Path(args[0], "job.json")))
    kind = args[1] or job["kind"]
    values = {key: job[key] for key in ("commit", "base", "branch", "key")}
    values.update(kind=kind, exclusive=job["exclusive"] or kind == "bench", bench_required=job["bench_required"],
                  queued_at=job["bench_queued_at" if args[1] == "bench" else "queued_at"])
    emit(values)
    print("argv=(" + shlex.join(job["argv"]) + ")")
elif command == "stamp":
    path = Path(args[0], "job.json")
    job = json.loads(read_file(path))
    job[args[1]] = (int if args[1].endswith("exit") else float)(args[2])
    write_file(path, json.dumps(job))
elif command == "phase":
    path = Path(args[0], "job.json")
    job = json.loads(read_file(path))
    job.setdefault(args[1] + "phases", []).append([args[2], time.time()])
    write_file(path, json.dumps(job))
elif command == "gate":
    for output in sorted(Path(args[0], "runs").iterdir(), reverse=True):
        path = output / "job.json"
        try:
            job = json.loads(read_file(path))
        except FileNotFoundError:
            continue
        if job["kind"] == "gate" and job["commit"] == args[1]:
            print(path.parent.name)
            break
    else:
        raise SystemExit("no gate recorded for this commit")
elif command == "task":
    for identifier, task in reversed(list(json.load(sys.stdin)["tasks"].items())):
        if "Done" not in task["status"] and (task["label"] == args[0] or (task["label"] or "").startswith(args[0] + "/")):
            print(identifier, "bench-" if task["label"].rsplit("/", 1)[-1] == "bench" else "")
            break
elif command == "admit":
    busy = []
    for path in Path(args[0], "projects").glob(str(Path("*", "slots", "*", "lock"))):
        with open_file(path, "a") as lock:
            try:
                fcntl.flock(lock, fcntl.LOCK_EX | fcntl.LOCK_NB)
            except BlockingIOError:
                busy.append(path)
    if len(busy) >= int(args[1]):
        print(busy[0])
elif command == "slots":
    repository, commit, count = args
    distances = []
    for slot in range(1, int(count) + 1):
        ref = f"refs/slots/{slot}"
        exists = subprocess.run(["git", "-C", repository, "show-ref", "--verify", "--quiet", ref]).returncode == 0
        distance = int(git(repository, "rev-list", "--count", f"{ref}...{commit}")) if exists else 10**12
        distances.append((distance, slot))
    print(" ".join(str(slot) for _, slot in sorted(distances)))
elif command == "image":
    source, context, project, repository = map(Path, args)
    config = project_config(repository)
    shutil.rmtree(context)
    context.mkdir()
    digest = hashlib.sha256()
    for item in config["image"]:
        path = source / item
        if Path(item).is_absolute() or ".." in Path(item).parts:
            raise ValueError(f"invalid image file: {item}")
        cursor = source
        for part in Path(item).parts:
            cursor /= part
            if cursor.is_symlink():
                raise ValueError(f"image path contains a symlink: {item}")
        if not path.is_file():
            raise ValueError(f"image file is missing: {item}")
        recorded = git(repository, "--literal-pathspecs", "ls-tree", "--format=%(objectmode)", "HEAD", "--", item)
        if recorded not in ("100644", "100755"):
            raise ValueError(f"image file is not a tracked regular file: {item}")
        mode = int(recorded, 8) & 0o777
        digest.update(item.encode() + b"\0")
        digest.update(mode.to_bytes(4, "big"))
        digest.update(hashlib.sha256(path.read_bytes()).digest())
        target = context / item
        target.parent.mkdir(parents=True, exist_ok=True)
        shutil.copy2(path, target)
        target.chmod(mode)
    emit(dict(image=f"ci-buddy-{project}:{digest.hexdigest()[:16]}", containerfile=config["image"][0],
              prepare=config.get("prepare", ""), wrapper=config.get("wrapper", ""),
              test=config["test"], gate=config.get("gate", ""), bench=config.get("bench", "")))
elif command == "git-view":
    repository, view = map(Path, args)
    if view.is_symlink() or not view.is_dir():
        view.unlink(missing_ok=True)
    else:
        shutil.rmtree(view)
    shutil.copytree(repository, view)
    for key, value in (("core.bare", "false"), ("core.worktree", "/work/src")):
        subprocess.run(["git", "--git-dir", str(view), "config", key, value], check=True)
elif command == "report":
    output, prefix, *timing = args
    output = Path(output)
    job = json.loads(read_file(output / "job.json"))
    stage = prefix.replace("-", "_")
    if not timing and stage + "started_at" not in job:
        with open_file(output / "report.txt", "a") as report:
            report.write(f"{prefix}runner ended before start; stats incomplete\n")
        sys.exit()
    started, ended, status = timing if timing else (job[stage + "started_at"], time.time(), 137)
    job[stage + "started_at"] = float(started)
    job[stage + "ended_at"], job[stage + "exit"] = float(ended), int(status)
    write_file(output / "job.json", json.dumps(job))
    with open_file(output / "report.txt", "a") as report:
        report.write(f"host_load={os.getloadavg()}\n")
        if job["kind"] == "test" and (output / "all-tests.json").exists():
            counts = []
            for name in ("all-tests", "selected-tests"):
                listing = json.loads(read_file(output / (name + ".json")))
                counts.append(sum(not case["ignored"] and case["filter-match"]["status"] == "matches" for suite in listing["rust-suites"].values() for case in suite["testcases"].values()))
            report.write(f"touched_packages={job['touched_packages']} selection={job['selection']} selected={counts[1]} total={counts[0]} scope=runnable\n")
        queued = job["bench_queued_at" if prefix else "queued_at"]
        report.write(f"{prefix}queue_s={float(started) - queued:.3f} run_s={float(ended) - float(started):.3f} exit={status}\n")
        phases = job.get(stage + "phases", []) + [["end", float(ended)]]
        for (name, begin), (_, end) in zip(phases, phases[1:]):
            report.write(f"{prefix}{name}_s={end - begin:.3f}\n")
        residual = float(ended) - float(started) - sum(b[1] - a[1] for a, b in zip(phases, phases[1:]))
        report.write(f"{prefix}unaccounted_s={residual:.3f}\n")
        totals = []
        for name in (prefix + "fetch-stats", prefix + "prepare-stats", prefix + "stats"):
            try:
                values = dict(line.replace("=", " ").split() for line in read_file(output / name).splitlines())
                totals.append({key: int(values[key]) for key in ("usage_usec", "memory_peak_bytes", "oom_kill")})
            except FileNotFoundError:
                pass
            except (ValueError, KeyError):
                report.write(f"{name} malformed; stats incomplete\n")
        if totals:
            report.write(f"cpu_s={sum(s['usage_usec'] for s in totals) / 1000000:.3f} peak_bytes={max(s['memory_peak_bytes'] for s in totals)} cap_killed={sum(s['oom_kill'] for s in totals)} scope=fetch+prepare+run peak_includes_file_cache=true\n")
        expected = 3 if any(name == "prepare" for name, _ in phases) else 2
        job[stage + "cpu_s"] = sum(s["usage_usec"] for s in totals) / 1000000 if len(totals) == expected else None
        job[stage + "peak_bytes"] = max(s["memory_peak_bytes"] for s in totals) if len(totals) == expected else None
        if len(totals) != expected:
            report.write("stats incomplete\n")
    write_file(output / "job.json", json.dumps(job))
