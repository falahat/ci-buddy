from pathlib import Path
import json
import shutil
import shlex
import subprocess
import sys
import tempfile
import time

root = Path(__file__).resolve().parent
client = [shutil.which("bash"), (root / "ci-buddy").as_posix()]
author = ["git", "-c", "user.name=example", "-c", "user.email=example@example.invalid"]
endpoint = sys.argv[1]
destination, separator, host_root = endpoint.partition(":")
assert separator and destination and host_root.startswith("/"), "provide destination:private-root"
sys.stdout.reconfigure(encoding="utf-8")

with tempfile.TemporaryDirectory(prefix="ci-buddy-example-") as directory:
    assert Path(directory).resolve().parent == Path(tempfile.gettempdir()).resolve()
    source = Path(directory, "example")
    shutil.copytree(root / "example", source)
    # Earlier invocations must not satisfy this invocation's cache-miss assertions.
    with (source / "arithmetic/src/lib.rs").open("a", encoding="utf-8") as stream:
        stream.write("\n// E2E candidate " + Path(directory).name + ".\n")
    config = source / "ci-buddy.toml"
    config.write_text(config.read_text().replace('host = ""', "host = " + json.dumps(endpoint)))
    subprocess.run(["git", "init", "-q", "-b", "main"], cwd=source, check=True)
    subprocess.run(["git", "add", "."], cwd=source, check=True)
    subprocess.run(author + ["commit", "-qm", "example"], cwd=source, check=True)

    def call(*arguments, status=0):
        result = subprocess.run(client + list(arguments), cwd=source, encoding="utf-8",
                                stdout=subprocess.PIPE, stderr=subprocess.STDOUT)
        print(result.stdout, end="", flush=True)
        assert result.returncode == status, (arguments, result.returncode, status)
        return result.stdout

    def launch(*arguments):
        return subprocess.Popen(client + list(arguments), cwd=source, encoding="utf-8",
                                stdout=subprocess.PIPE, stderr=subprocess.STDOUT)

    def announced(process, marker=None):
        run = None
        for line in process.stdout:
            print(line, end="", flush=True)
            if line.startswith("run="):
                run = line.split()[0].split("=", 1)[1]
            if line.strip() == marker or marker is None and run is not None:
                break
        assert run is not None and run.isdecimal(), run
        return run

    probe = """import fcntl,json,subprocess,sys,tomllib
from pathlib import Path
root = Path(sys.argv[1])
assert root.is_dir() and root.resolve() != (Path.home() / '.ci-buddy').resolve(), 'use a private root'
project = root / 'projects' / 'example'
out = project / 'runs' / sys.argv[2] if sys.argv[2] else project
"""

    def run_probe(action, run=""):
        command = shlex.join(["python3", "-c", probe + action, host_root, run])
        return subprocess.check_output(["ssh", destination, command], encoding="utf-8")

    def refs():
        return run_probe("print(subprocess.check_output(['git','--git-dir',str(project/'git'),"
                         "'for-each-ref','--format=%(refname)','refs/ci-buddy'],text=True))")

    def run_id(output):
        return next(line.split()[0].split("=")[1] for line in output.splitlines() if line.startswith("run="))

    run_probe("assert (root / 'bin' / 'ci-buddy').is_file()")
    assert "argument with spaces" in call("run", "--", "printf", "%s\\n", "argument with spaces")
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
    started = time.monotonic()
    assert "reused_from=" + test_run in call("test") and time.monotonic() - started < 5
    assert "reused_from=" not in call("test", "--rerun")
    assert "reused_from=" not in call("test", "-E", "all()"), "changed argv reused an old result"
    call("run", "--", "sh", "-c", "test ! -e /run/secrets && test -f \"$CI_BUDDY_OUT/prepare.txt\"")
    call("run", "--", "python3", "-c", "import socket; socket.create_connection(('192.0.2.1',9))", status=1)

    subprocess.run(["git", "switch", "--quiet", "--detach"], cwd=source, check=True)
    detached = run_id(call("run", "--", "true"))
    run_probe("assert json.loads((out / 'job.json').read_text())['branch'] == ''", detached)
    subprocess.run(["git", "switch", "--quiet", "main"], cwd=source, check=True)

    running = launch("run", "--", "python3", "-c",
                     "import signal; print('cancel-ready',flush=True); signal.pause()")
    run = announced(running, "cancel-ready")
    try:
        assert running.poll() is None
        assert run in call("status")
        mode = subprocess.check_output(["ssh", destination, "podman", "inspect", "--format",
                                        "{{.HostConfig.NetworkMode}}", "ci-buddy-" + run], encoding="utf-8")
        assert mode.strip() == "none", mode
    finally:
        if running.poll() is None:
            call("cancel", run)
        running.communicate()
    assert running.returncode != 0
    call("cancel", run)
    run_probe("""slot = (out / 'slot').read_text().strip()
with (project / 'slots' / slot / 'lock').open('r+') as lock:
    fcntl.flock(lock,fcntl.LOCK_EX|fcntl.LOCK_NB)""", run)
    call("run", "--", "printf", "slot-released\\n")

    # A rejected output link must leave the host-side data canary unchanged.
    call("run", "--exclusive", "--", "sh", "-c",
         'printf readable >"$CI_BUDDY_DATA/probe"; ln -s ../../data/probe "$CI_BUDDY_OUT/report.txt"', status=1)
    call("run", "--", "sh", "-c",
         '! printf wrong >"$CI_BUDDY_DATA/probe"; test "$(cat "$CI_BUDDY_DATA/probe")" = readable')
    commit = subprocess.check_output(["git", "rev-parse", "HEAD"], cwd=source, encoding="utf-8").strip()
    gate = call("gate", "--rerun")
    run = run_id(gate)
    verifying = launch("verify", commit)
    try:
        assert verifying.stdout.readline().startswith("waiting bench_task=")
        run_probe("assert not (out / 'bench-exit').exists()\n(out / 'release').write_text('release')", run)
        report = verifying.communicate()[0]
    finally:
        if verifying.poll() is None:
            call("cancel", run)
            verifying.communicate()
    assert verifying.returncode == 0 and "gate green" in report and "bench green " + run in report, report
    config.write_text("\n".join('gate = "false"' if line.startswith("gate = ") else line
                                for line in original.splitlines()) + "\n")
    subprocess.run(author + ["commit", "-qam", "red gate"], cwd=source, check=True)
    red = call("gate", status=1)
    run = run_id(red)
    assert "bench_task=" not in red, red
    run_probe("assert not (out / 'bench-task').exists()", run)
    commit = subprocess.check_output(["git", "rev-parse", "HEAD"], cwd=source, encoding="utf-8").strip()
    call("verify", commit, status=1)
    assert "reused_from=" not in call("test"), "changed tree reused an old result"

    # Fill this root's jobs group with only our own holders, then cancel a queued ref.
    count = int(run_probe("print(tomllib.loads((root / 'host.conf').read_text())['jobs'])").strip())
    holders = []
    queued = None
    twins = []
    try:
        for _ in range(count):
            holder = launch("run", "--", "python3", "-c",
                            "import signal; print('holder-ready',flush=True); signal.pause()")
            holders.append((holder, announced(holder, "holder-ready")))
        queued = launch("run", "--", "true")
        queued_run = announced(queued)
        run_probe("""assert not (out / 'slot').exists(), 'job fetched before cancellation'
assert subprocess.run(['git','--git-dir',str(project/'git'),'show-ref','--verify','--quiet',
                       'refs/ci-buddy/'+sys.argv[2]]).returncode == 0""", queued_run)
        call("cancel", queued_run)
        queued.communicate()
        assert queued.returncode == 137, queued.returncode
        run_probe("""assert subprocess.run(['git','--git-dir',str(project/'git'),'show-ref','--verify','--quiet',
                       'refs/ci-buddy/'+sys.argv[2]]).returncode == 1""", queued_run)
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
    twin_outputs = [twin.communicate()[0] for twin in twins]
    assert all(twin.returncode == 0 for twin in twins) and all("reused_from=" not in text for text in twin_outputs)
    for question in (("test", "arithmetic::adds_unequal_values"), ("failures", "--branch", "main"),
                     ("slowest",), ("change", commit), ("flaky",), ("gates",)):
        started = time.monotonic()
        answer = json.loads(call("history", *question))
        assert time.monotonic() - started < 2 and isinstance(answer, (dict, list))
        assert answer or question[0] in ("failures", "flaky", "gates"), question
    assert refs() == baseline_refs, "a completed or cancelled candidate ref remains"
print("example end-to-end: green")
