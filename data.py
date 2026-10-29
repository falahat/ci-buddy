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


def read_file(path, limit=16 * 1024 * 1024):
    with open_file(path) as stream:
        content = stream.read(limit + 1)
        if len(content.encode()) > limit:
            raise ValueError('file exceeds byte limit')
        return content


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
    allowed = {"name", "main", "image", "prepare", "wrapper", "gate", "test", "bench"}
    if config.keys() - allowed:
        raise ValueError(f"unknown project settings: {config.keys() - allowed}")
    for name in ("test", "gate", "bench"):
        if name == "test" or name in config:
            if not isinstance(config[name], str) or not config[name].strip():
                raise ValueError(f"{name} requires a nonempty command")
    return config


def make_job(folder, kind, commit, base, branch, submitter, argv):
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
               argv=argv, key="", rerun=rerun, queued_at=time.time(),
               steps=[dict(name="profiling" if kind == "bench" else "correctness",
                           kind=kind, exclusive=exclusive, argv=argv)])
    if kind == "gate" and "bench" in config:
        job["steps"].append(dict(name="profiling", kind="bench", exclusive=True, argv=[]))
    if kind != "run":
        revision = read_file(Path(folder).parents[1] / "bin/revision").strip()
        inputs = [kind, job["tree"], argv, exclusive, base if kind == "test" and not argv else "", revision]
        job["key"] = hashlib.sha256(json.dumps(inputs, sort_keys=True).encode()).hexdigest()
    return job


def main():
    command, *args = sys.argv[1:]
    if command == "host":
        config = host_config(Path(args[0]))
        emit({key: config[key] for key in ("jobs", "slots", "memory", "cpus", "min_free_gb", "slot_max_gb", "keep_days")})
    elif command == "affected":
        path, output, repository = map(Path, args)
        job = json.loads(read_file(path))
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
        write_file(path, json.dumps(job))
        emit(dict(affected=expression if not job["argv"] else ""))
    elif command in ("load", "stamp", "finish", "phase", "slot"):
        path = Path(args[0])
        job = json.loads(read_file(path))
        if command == "slot":
            job["slot"] = int(args[1])
        else:
            step = job["steps"][int(args[1])]
            if command == "load":
                emit(dict(commit=job["commit"], base=job["base"], **{key: step[key] for key in ("name", "kind", "exclusive")}))
                print("argv=(" + shlex.join(step["argv"]) + ")")
                print("chosen=" + str(job.get("slot", "")))
                return
            if command == "finish":
                step.update(ended_at=float(args[2]), exit=int(args[3]))
            elif command == "phase":
                step.setdefault("phases", []).append([args[2], time.time()])
            else:
                step[args[2]] = (int if args[2] == "exit" else float)(args[3])
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


if __name__ == "__main__":
    main()
