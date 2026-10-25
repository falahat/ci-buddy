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


command, *args = sys.argv[1:]
if command == "host":
    config = tomllib.loads(Path(args[0], "host.conf").read_text())
    allowed = {"jobs", "slots", "memory", "cpus", "min_free_gb", "slot_max_gb", "keep_days", "workers"}
    if config.keys() - allowed:
        raise ValueError(f"unknown host settings: {config.keys() - allowed}")
    emit({key: config[key] for key in ("jobs", "slots", "memory", "cpus", "min_free_gb", "keep_days")})
elif command == "job":
    folder, run, kind, commit, base, branch, submitter, *argv = args
    if kind not in ("run", "test"):
        raise ValueError(f"unknown job kind: {kind}")
    if kind == "run" and argv[:1] == ["--"]:
        argv = argv[1:]
    if kind == "run" and not argv:
        raise ValueError("run requires a command")
    job = dict(kind=kind, commit=commit, base=base, branch=branch, submitter=submitter,
               tree=git(Path(folder, "git"), "rev-parse", commit + "^{tree}"),
               argv=argv, key="", queued_at=time.time())
    Path(folder, "runs", run, "job.json").write_text(json.dumps(job))
elif command == "load":
    job = json.loads(Path(args[0], "job.json").read_text())
    emit({key: job[key] for key in ("kind", "commit", "base", "branch", "queued_at")})
    print("argv=(" + shlex.join(job["argv"]) + ")")
elif command == "start":
    path = Path(args[0], "job.json")
    job = json.loads(path.read_text())
    job["started_at"] = float(args[1])
    path.write_text(json.dumps(job))
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
    config = tomllib.loads((source / "ci-buddy.toml").read_text())
    allowed = {"name", "host", "main", "image", "prepare", "wrapper", "gate", "test", "bench"}
    if config.keys() - allowed:
        raise ValueError(f"unknown project settings: {config.keys() - allowed}")
    if not isinstance(config["test"], str) or not config["test"].strip():
        raise ValueError("test requires a nonempty command")
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
              prepare=config.get("prepare", "true"), wrapper=config.get("wrapper", ""), test=config["test"]))
elif command == "report":
    output, *timing = args
    output = Path(output)
    job = json.loads((output / "job.json").read_text())
    if not timing and "started_at" not in job:
        (output / "report.txt").write_text("runner ended before start; stats incomplete\n")
        sys.exit()
    started, ended, status = timing if timing else (job["started_at"], time.time(), 137)
    with (output / "report.txt").open("a") as report:
        report.write(f"queue_s={float(started) - job['queued_at']:.3f} run_s={float(ended) - float(started):.3f} exit={status}\n")
        totals = []
        for name in ("fetch-stats", "stats"):
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
