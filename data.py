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


def discard(repository, run):
    for name in (run, 'base-' + run):
        git(repository, 'update-ref', '-d', 'refs/ci-buddy/' + name)


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


def read_file(path, limit=-1):
    with open_file(path) as stream:
        return stream.read(limit)


def write_file(path, content):
    temporary = path.with_suffix(".tmp")
    with open_file(temporary, "w") as stream:
        stream.write(content)
    temporary.replace(path)


def host_config(root):
    config = tomllib.loads((root / 'host.conf').read_text())
    required = {'jobs', 'slots', 'memory', 'cpus', 'min_free_gb', 'slot_max_gb', 'fetch_hosts'}
    missing = required - config.keys()
    if missing:
        raise ValueError('host.conf requires ' + ', '.join(sorted(missing)))
    if config.keys() - required - {'keep_days'}:
        raise ValueError('unknown host.conf settings')
    for name in ('jobs', 'slots', 'min_free_gb', 'slot_max_gb', 'keep_days'):
        value = config.get(name, 90) if name == 'keep_days' else config[name]
        if type(value) is not int or value <= 0:
            raise ValueError(f'{name} must be a positive integer')
    if not isinstance(config['memory'], str) or not config['memory'].strip() or not isinstance(config['cpus'], str):
        raise ValueError('memory and cpus must be strings; only cpus may be empty')
    if not isinstance(config['fetch_hosts'], list) or any(not isinstance(endpoint, str) or ':' not in endpoint for endpoint in config['fetch_hosts']):
        raise ValueError('fetch_hosts must be an array of destination:root strings')
    config.setdefault('keep_days', 90)
    return config


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


def output(out, job):
    write_file(out / 'job.json', json.dumps(job))
    for name in ('task', 'slot', 'exit', 'bench-task', 'bench-exit', 'runner.log'):
        write_file(out / name, '')


def make_job(folder, run, kind, commit, base, branch, submitter, argv):
    config = project_config(Path(folder, "git"), commit)
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
               bench_required=kind == "gate" and "bench" in config)
    if kind != "run":
        revision = read_file(Path(folder).parents[1] / "bin/revision").strip()
        inputs = [kind, job["tree"], argv, base if kind == "test" and not argv else "", revision]
        job["key"] = hashlib.sha256(json.dumps(inputs, sort_keys=True).encode()).hexdigest()
    output(Path(folder, 'runs', run), job)
    return job


def reuse_finished(folder, run):
    out = folder / "runs" / run
    job = json.loads(read_file(out / "job.json"))
    if job["key"] and not job["rerun"]:
        database = history.db(folder)
        for row in database.execute('SELECT * FROM runs WHERE key=? AND reused_from IS NULL ORDER BY id DESC', (job["key"],)):
            prior = dict(row)
            if prior['exit'] is None:
                if read_file(folder / 'runs' / str(prior['id']) / 'bench-task').strip():
                    return str(prior['id'])
                continue
            complete = database.execute("SELECT 1 FROM results JOIN tests ON tests.id=results.test WHERE run=? AND name='phase/bench'", (prior["id"],)).fetchone()
            if job["bench_required"] and prior["exit"] == 0 and not complete:
                continue
            original = prior["id"]
            previous_out = folder / 'runs' / str(original)
            if previous_out.exists() and (not read_file(previous_out / 'exit').strip()
                    or read_file(previous_out / 'bench-task').strip() and not read_file(previous_out / 'bench-exit').strip()):
                continue
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
            return str(original)


def gate_run(folder, commit):
    for output in sorted(Path(folder, "runs").iterdir(), reverse=True):
        path = output / "job.json"
        try:
            job = json.loads(read_file(path))
        except FileNotFoundError:
            continue
        if job["kind"] == "gate" and job["commit"] == commit:
            return path.parent.name
    else:
        raise ValueError("no gate recorded for this commit")


def record(output):
    path = output / "job.json"
    job = json.loads(read_file(path))
    job["junit_ingested_at"] = history.record(output.parents[1], output, read_file)
    write_file(path, json.dumps(job))
    return (output.parents[1] / "history.sqlite").stat().st_size


def report(output, prefix, *timing):
    output = Path(output)
    job = json.loads(read_file(output / "job.json"))
    stage = prefix.replace("-", "_")
    if not timing and stage + "started_at" not in job:
        with open_file(output / "report.txt", "a") as report:
            report.write(f"{prefix}runner ended before start; stats incomplete\n")
        return
    started, ended, status = timing if timing else (job[stage + "started_at"], time.time(), job.get(stage + 'exit', 137))
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


def main():
    command, *args = sys.argv[1:]
    if command == "read":
        sys.stdout.write(read_file(args[0]))
    elif command == "write":
        write_file(Path(args[0]), args[1] + "\n")
    elif command == "host":
        config = host_config(Path(args[0]))
        emit({key: config[key] for key in ("jobs", "slots", "memory", "cpus", "min_free_gb", "slot_max_gb", "keep_days")})
    elif command == "record":
        print(f"history_bytes={record(Path(args[0]))}")
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
        values = {key: job[key] for key in ("commit", "base")}
        values.update(kind=kind, exclusive=job["exclusive"] or kind == "bench")
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
            if Path(item).is_absolute() or ".." in Path(item).parts:
                raise ValueError(f"invalid image file: {item}")
            recorded = git(repository, "--literal-pathspecs", "ls-tree", "--format=%(objectmode) %(objectname)", "HEAD", "--", item)
            mode, blob = recorded.split()
            if mode not in ("100644", "100755"):
                raise ValueError(f"image file is not a tracked regular file: {item}")
            digest.update(item.encode() + b"\0" + recorded.encode())
            target = context / item
            target.parent.mkdir(parents=True, exist_ok=True)
            target.write_bytes(subprocess.check_output(["git", "-C", str(repository), "cat-file", "blob", blob]))
            target.chmod(int(mode, 8) & 0o777)
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
        report(*args)


if __name__ == "__main__":
    main()
