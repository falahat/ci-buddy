from pathlib import Path
import json
import os
import re
import shutil
import shlex
import subprocess
import sys
import tempfile
import time
import tomllib

root = Path(__file__).resolve().parent
client = [shutil.which("bash"), (root / "ci-buddy").as_posix()]
def client_command(arguments):
    # Windows native argv quoting differs from MSYS; let Bash quote its own argv.
    return [client[0], '-lc', shlex.join(['bash', client[1], *arguments])] if os.name == 'nt' else client + list(arguments)
def administer(host, arguments):
    return arguments if host == 'local' else ['ssh', host, shlex.join(arguments)]
author = ["git", "-c", "user.name=example", "-c", "user.email=example@example.invalid"]
endpoint = sys.argv[1]
destination, separator, host_root = endpoint.partition(":")
assert separator and destination and host_root.startswith("/"), "provide destination:private-root"
worker_destination, worker_separator, worker_root = (sys.argv[2] if len(sys.argv) > 2 else endpoint).partition(':')
assert worker_separator and worker_root.startswith('/'), 'provide a private worker endpoint'
sys.stdout.reconfigure(encoding="utf-8")

with tempfile.TemporaryDirectory(prefix="ci-buddy-example-") as directory:
    assert Path(directory).resolve().parent == Path(tempfile.gettempdir()).resolve()
    os.environ['GIT_CONFIG_GLOBAL'] = str(Path(directory, 'gitconfig'))
    subprocess.run(['git', 'config', '--global', 'ci-buddy.host', endpoint], check=True)
    source = Path(directory, "example")
    shutil.copytree(root / "example", source)
    # Earlier invocations must not satisfy this invocation's cache-miss assertions.
    with (source / "arithmetic/src/lib.rs").open("a", encoding="utf-8") as stream:
        stream.write("\n// E2E candidate " + Path(directory).name + ".\n")
    config = source / "ci-buddy.toml"
    subprocess.run(["git", "init", "-q", "-b", "main"], cwd=source, check=True)
    subprocess.run(["git", "add", "."], cwd=source, check=True)
    subprocess.run(author + ["commit", "-qm", "example"], cwd=source, check=True)

    def call(*arguments, status=0):
        result = subprocess.run(client_command(arguments), cwd=source, encoding="utf-8",
                                stdout=subprocess.PIPE, stderr=subprocess.STDOUT)
        print(result.stdout, end="", flush=True)
        assert result.returncode == status, (arguments, result.returncode, status)
        if arguments[0] in ("history", "status"):
            return json.dumps(json.JSONDecoder().raw_decode(result.stdout.lstrip())[0])
        return result.stdout

    def launch(*arguments):
        return subprocess.Popen(client_command(arguments), cwd=source, encoding="utf-8",
                                stdout=subprocess.PIPE, stderr=subprocess.STDOUT)

    def announced(process, marker=None):
        run = None
        for line in process.stdout:
            print(line, end="", flush=True)
            if line.startswith("run="):
                run = line.split()[0].split("=", 1)[1]
            if line.strip() == marker or marker is None and run is not None:
                break
        assert run is not None and re.fullmatch(r'j_[0-9A-HJKMNP-TV-Z]{26}', run), run
        return run

    probe = """import fcntl,json,subprocess,sys,tomllib
from pathlib import Path
root = Path(sys.argv[1])
assert root.is_dir() and root.resolve() != (Path.home() / '.ci-buddy').resolve(), 'use a private root'
project = root / 'projects' / 'example'
sys.path.insert(0,str(root/'bin'))
import history
def document():
    return json.loads(history.run(project,sys.argv[2])['document'])
"""

    def run_probe(action, run=""):
        return subprocess.check_output(administer(destination, ["python3", "-c", probe + action, host_root, run]),
                                       encoding="utf-8")

    def worker_probe(action, run):
        script = probe + "work = root / 'dispatch/work' / sys.argv[2]\nout = work / 'artifacts/out'\n"
        return subprocess.check_output(administer(worker_destination, ["python3", "-c", script + action, worker_root, run]),
                                       encoding="utf-8")

    def refs():
        return run_probe("print(subprocess.check_output(['git','--git-dir',str(project/'git'),"
                         "'for-each-ref','--format=%(refname)','refs/ci-buddy'],text=True))")

    def run_id(output):
        return next(line.split()[0].split("=")[1] for line in output.splitlines() if line.startswith("run="))

    run_probe("assert (root / 'bin' / 'ci-buddy').is_file()")
    assert "argument with spaces\n" in call("run", "--", "printf", "%s\\n", "argument with spaces")
    baseline_refs = refs()
    assert "run requires a command" in call("run", "--", status=1)
    assert refs() == baseline_refs, "pre-fetch validation leaked a candidate ref"
    original = config.read_text()
    config.write_text(original + '\nwrappper = "invalid"\n')
    try:
        assert "unknown project settings" in call("run", "--", "true", status=1)
        assert refs() == baseline_refs, "pre-fetch configuration failure leaked a candidate ref"
    finally:
        config.write_text(original)
    call("run", "--", "sh", "-c", "exit 23", status=23)
    test_run = run_id(call("test", "--rerun"))
    assert "passed" in call("log", test_run)
    base = subprocess.check_output(["git", "rev-parse", "HEAD"], cwd=source, encoding="utf-8").strip()
    for name, expected in (("arithmetic/src/lib.rs", 2), ("consumer/src/lib.rs", 1), ("ci-buddy.toml", 3)):
        path = source / name
        before = path.read_text()
        path.write_text(before + ("\n# shared change\n" if name.endswith("toml") else "\n// package change\n"))
        try:
            assert f"selected={expected} total=3" in call("test", "--since", base)
        finally:
            path.write_text(before)
    started = time.monotonic()
    assert "reused_from=" + test_run in call("test") and time.monotonic() - started < 5
    assert "reused_from=" not in call("test", "--rerun")
    assert "reused_from=" not in call("test", "-E", "all()"), "changed argv reused an old result"
    call("run", "--", "sh", "-c", "test ! -e /run/secrets && test ! -e /work/token && test -f \"$CI_BUDDY_OUT/prepare.txt\"")
    call("run", "--", "python3", "-c", "import socket; socket.create_connection(('192.0.2.1',9))", status=1)

    subprocess.run(["git", "switch", "--quiet", "--detach"], cwd=source, check=True)
    detached = run_id(call("run", "--", "true"))
    run_probe("assert document()['job']['branch'] == ''", detached)
    subprocess.run(["git", "switch", "--quiet", "main"], cwd=source, check=True)

    running = launch("run", "--", "python3", "-c",
                     "import signal; print('cancel-ready',flush=True); signal.pause()")
    run = announced(running, "cancel-ready")
    try:
        assert running.poll() is None
        assert run in call("status")
        container = json.loads(subprocess.check_output(administer(worker_destination,
            ["podman", "inspect", "ci-buddy-" + run]), encoding="utf-8"))[0]
        assert container['HostConfig']['NetworkMode'] == 'none', container
        slot = next(str(Path(mount['Source']).parent) for mount in container['Mounts'] if mount['Destination'] == '/work/src')
    finally:
        if running.poll() is None:
            call("cancel", run)
        running.communicate()
    assert running.returncode != 0
    call("cancel", run)
    worker_probe("with Path(" + repr(slot + '/lock') + ").open('r+') as lock:\n    fcntl.flock(lock,fcntl.LOCK_EX|fcntl.LOCK_NB)", run)
    call("run", "--", "printf", "slot-released\\n")

    # A rejected output link must leave the host-side data canary unchanged.
    rejected = run_id(call("run", "--exclusive", "--", "sh", "-c",
         'printf readable >"$CI_BUDDY_DATA/probe"; ln -s ../../../../../projects/example/data/probe "$CI_BUDDY_OUT/report.txt"', status=1))
    run_probe("""sys.path.insert(0,str(root/'bin'))
import control
with control.client(root) as queue:
    view = queue.job(sys.argv[2])
assert view['state'] == 'FAILED' and view.get('reason')
assert control.history.run(project,sys.argv[2])['exit'] is None""", rejected)
    call("run", "--", "sh", "-c",
         '! printf wrong >"$CI_BUDDY_DATA/probe"; test "$(cat "$CI_BUDDY_DATA/probe")" = readable')
    commit = subprocess.check_output(["git", "rev-parse", "HEAD"], cwd=source, encoding="utf-8").strip()
    gate = launch("gate", "--rerun")
    run = announced(gate, "correctness_exit=0")
    try:
        assert gate.poll() is None, 'gate returned before profiling completed'
        assert any(row['job_id'] == run and row['state'] == 'RUNNING' and row.get('progress') == dict(phase='profiling', correctness_exit=0) for row in json.loads(call('status'))['jobs'])
        worker_probe("(out / 'release').write_text('release')", run)
        gate.communicate()
    finally:
        if gate.poll() is None:
            call("cancel", run)
            gate.communicate()
    report = call("verify", commit)
    assert gate.returncode == 0 and "gate green" in report and "bench green " + run in report, report
    config.write_text("\n".join('gate = "false"' if line.startswith("gate = ") else line
                                for line in original.splitlines()) + "\n")
    subprocess.run(author + ["commit", "-qam", "red gate"], cwd=source, check=True)
    red = call("gate", status=1)
    run = run_id(red)
    run_probe("job = document()['result']\nassert job['exit'] == 1 and len(job['steps']) == 1 and job['steps'][0]['exit'] == 1", run)
    commit = subprocess.check_output(["git", "rev-parse", "HEAD"], cwd=source, encoding="utf-8").strip()
    call("verify", commit, status=1)
    assert "reused_from=" not in call("test"), "changed tree reused an old result"

    # An exclusive holder keeps these queued workers from blocking other exclusives.
    count = int(run_probe("print(tomllib.loads((root / 'host.conf').read_text())['jobs'])").strip())
    barrier = launch("run", "--exclusive", "--", "python3", "-c",
                     "import signal; print('holder-ready',flush=True); signal.pause()")
    barrier_run = announced(barrier, "holder-ready")
    holders = []
    queued = None
    twins = []
    try:
        for _ in range(count):
            holder = launch("run", "--", "true")
            holders.append((holder, announced(holder)))
        queued = launch("run", "--", "true")
        queued_run = announced(queued)
        run_probe("""sys.path.insert(0,str(root/'bin'))
import control
with control.client(root) as queue:
    view = queue.job(sys.argv[2])
    assert view['state'] == 'QUEUED' and not view.get('runner_id')
spec = control.blob(root, address=view['invocation_address'])
bundle = root / 'containers' / (sys.argv[2] + '.bundle')
control.results.cas(root, 'get', spec['inputs']['source']['address'], bundle)
assert subprocess.run(['git','-C',str(project/'git'),'bundle','verify',str(bundle)]).returncode == 0
bundle.unlink()""", queued_run)
        assert refs() == baseline_refs, 'queued submission retained source refs'
        call("cancel", queued_run)
        queued.communicate()
        assert queued.returncode == 1, queued.returncode
        assert refs() == baseline_refs, 'cancelled submission retained source refs'
        with (source / "arithmetic/src/lib.rs").open("a", encoding="utf-8") as stream:
            stream.write("\n// Reuse control " + Path(directory).name + ".\n")
        subprocess.run(author + ["commit", "-qam", "reuse control"], cwd=source, check=True)
        twins = [launch("test", "-E", "test(adds_unequal_values)") for _ in range(2)]
        assert len({announced(twin) for twin in twins}) == 1, "identical queued requests ran twice"
    finally:
        if queued is not None and queued.poll() is None:
            call("cancel", queued_run)
            queued.communicate()
        for holder, holder_run in holders:
            if holder.poll() is None:
                call("cancel", holder_run)
            holder.communicate()
        if barrier.poll() is None:
            call("cancel", barrier_run)
        barrier.communicate()
    twin_outputs = [twin.communicate()[0] for twin in twins]
    assert all(twin.returncode == 0 for twin in twins) and all("reused_from=" not in text for text in twin_outputs)
    for question in (("test", "arithmetic::adds_unequal_values"), ("failures", "--branch", "main"),
                     ("slowest",), ("change", commit), ("flaky",), ("gates",)):
        started = time.monotonic()
        answer = json.loads(call("history", *question))["answer"]
        assert time.monotonic() - started < 2 and isinstance(answer, (dict, list))
        assert answer or question[0] in ("failures", "flaky", "gates"), question
        if question[0] == "test": assert answer["runs"] > 0
    if len(sys.argv) > 2:
        # The fixture's only consumers belong to the separately installed worker.
        worker = call("test", "--rerun", "-E", "package(=consumer)")
        run = run_id(worker)
        run_probe("assert document()['node']", run)
        commit = subprocess.check_output(["git", "rev-parse", "HEAD"], cwd=source, encoding="utf-8").strip()
        answer = json.loads(call("history", "change", commit))["answer"]
        assert answer, "worker result did not return to the coordinator history"
        readonly = """import errno,os,sys
from pathlib import Path
print('worker-exit-three-once',flush=True)
try:
    descriptor = os.open(Path(os.environ['CI_BUDDY_OUT']) / 'job.json', os.O_WRONLY)
except OSError as error:
    assert error.errno == errno.EROFS, error
else:
    os.close(descriptor)
    raise AssertionError('writable job metadata')
sys.exit(3)"""
        result = call("run", "--", "python3", "-c", readonly, status=3)
        assert result.count('worker-exit-three-once') == 1, result
    assert refs() == baseline_refs, "a completed or cancelled candidate ref remains"
print("example end-to-end: green")
