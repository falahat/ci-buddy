import hashlib
import json
from pathlib import Path
import shlex
import shutil
import subprocess
import sys
import time
import tomllib


def git(folder, *args):
    return subprocess.check_output(["git", "-C", str(folder), *args], text=True).strip()


def emit(values):
    for key, value in values.items():
        print(f"{key}={shlex.quote(str(value))}")


def write_job(path, job):
    temporary = path.with_suffix(".tmp")
    temporary.write_text(json.dumps(job))
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
if command == "host":
    config = tomllib.loads(Path(args[0], "host.conf").read_text())
    allowed = {"jobs", "slots", "memory", "cpus", "min_free_gb", "slot_max_gb", "keep_days", "workers"}
    if config.keys() - allowed:
        raise ValueError(f"unknown host settings: {config.keys() - allowed}")
    emit({key: config[key] for key in ("jobs", "slots", "memory", "cpus", "min_free_gb", "keep_days")})
elif command == "job":
    folder, run, kind, commit, base, branch, submitter, *argv = args
    config = project_config(Path(folder, "git"), commit)
    if kind not in ("run", "test", "gate", "bench"):
        raise ValueError(f"unknown job kind: {kind}")
    exclusive = kind == "bench" or kind == "run" and argv[:1] == ["--exclusive"]
    if kind == "run" and exclusive:
        argv = argv[1:]
    if kind == "run" and argv[:1] == ["--"]:
        argv = argv[1:]
    if kind == "run" and not argv:
        raise ValueError("run requires a command")
    if kind in ("gate", "bench") and (kind not in config or argv):
        raise ValueError(f"{kind} requires its configured command and no arguments")
    job = dict(kind=kind, commit=commit, base=base, branch=branch, submitter=submitter,
               tree=git(Path(folder, "git"), "rev-parse", commit + "^{tree}"),
               argv=argv, key="", queued_at=time.time(), exclusive=exclusive,
               bench_required=kind == "gate" and "bench" in config)
    write_job(Path(folder, "runs", run, "job.json"), job)
    print("exclusive" if exclusive else "jobs")
elif command == "load":
    job = json.loads(Path(args[0], "job.json").read_text())
    kind = args[1] or job["kind"]
    values = {key: job[key] for key in ("commit", "base", "branch")}
    values.update(kind=kind, exclusive=job["exclusive"] or kind == "bench", bench_required=job["bench_required"],
                  queued_at=job["bench_queued_at" if args[1] == "bench" else "queued_at"])
    emit(values)
    print("argv=(" + shlex.join(job["argv"]) + ")")
elif command == "stamp":
    path = Path(args[0], "job.json")
    job = json.loads(path.read_text())
    job[args[1]] = float(args[2])
    write_job(path, job)
elif command == "gate":
    for output in sorted(Path(args[0], "runs").iterdir(), reverse=True):
        path = output / "job.json"
        if not path.is_file():
            continue
        job = json.loads(path.read_text())
        if job["kind"] == "gate" and job["commit"] == args[1]:
            print(path.parent.name)
            break
    else:
        raise SystemExit("no gate recorded for this commit")
elif command == "task":
    for identifier, task in json.load(sys.stdin)["tasks"].items():
        if task["label"] == args[0]:
            print(identifier)
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
    source, context, project = map(Path, args)
    config = project_config(source)
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
        digest.update(item.encode() + b"\0")
        digest.update((path.stat().st_mode & 0o777).to_bytes(4, "big"))
        digest.update(hashlib.sha256(path.read_bytes()).digest())
        target = context / item
        target.parent.mkdir(parents=True, exist_ok=True)
        shutil.copy2(path, target)
    emit(dict(image=f"ci-buddy-{project}:{digest.hexdigest()[:16]}", containerfile=config["image"][0],
              prepare=config.get("prepare", "true"), wrapper=config.get("wrapper", ""),
              test=config["test"], gate=config.get("gate", ""), bench=config.get("bench", "")))
elif command == "report":
    output, prefix, *timing = args
    output = Path(output)
    job = json.loads((output / "job.json").read_text())
    stage = prefix.replace("-", "_")
    if not timing and stage + "started_at" not in job:
        with (output / "report.txt").open("a") as report:
            report.write(f"{prefix}runner ended before start; stats incomplete\n")
        sys.exit()
    started, ended, status = timing if timing else (job[stage + "started_at"], time.time(), 137)
    job[stage + "started_at"] = float(started)
    job[stage + "ended_at"], job[stage + "exit"] = float(ended), int(status)
    with (output / "report.txt").open("a") as report:
        queued = job["bench_queued_at" if prefix else "queued_at"]
        report.write(f"{prefix}queue_s={float(started) - queued:.3f} run_s={float(ended) - float(started):.3f} exit={status}\n")
        totals = []
        for name in (prefix + "fetch-stats", prefix + "stats"):
            if (output / name).exists():
                try:
                    values = dict(line.replace("=", " ").split() for line in (output / name).read_text().splitlines())
                    totals.append({key: int(values[key]) for key in ("usage_usec", "memory_peak_bytes", "oom_kill")})
                except (ValueError, KeyError):
                    report.write(f"{name} malformed; stats incomplete\n")
        if totals:
            report.write(f"cpu_s={sum(s['usage_usec'] for s in totals) / 1000000:.3f} peak_bytes={max(s['memory_peak_bytes'] for s in totals)} cap_killed={sum(s['oom_kill'] for s in totals)} scope=fetch+run\n")
        if len(totals) != 2:
            report.write("stats incomplete\n")
    write_job(output / "job.json", job)
