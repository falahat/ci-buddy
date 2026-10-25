from pathlib import Path
import shutil
import shlex
import subprocess
import sys
import tempfile

root = Path(__file__).resolve().parent
client = [shutil.which("bash"), (root / "ci-buddy").as_posix()]
sys.stdout.reconfigure(encoding="utf-8")
with tempfile.TemporaryDirectory(prefix="ci-buddy-example-") as directory:
    assert Path(directory).resolve().parent == Path(tempfile.gettempdir()).resolve()
    source = Path(directory, "example")
    shutil.copytree(root / "example", source)
    config = source / "ci-buddy.toml"
    config.write_text(config.read_text().replace('host = ""', f'host = "{sys.argv[1]}"'))
    subprocess.run(["git", "init", "-q", "-b", "main"], cwd=source, check=True)
    subprocess.run(["git", "add", "."], cwd=source, check=True)
    subprocess.run(["git", "-c", "user.name=example", "-c", "user.email=example@example.invalid",
                    "commit", "-qm", "example"], cwd=source, check=True)

    def call(*arguments, status=0):
        result = subprocess.run(client + list(arguments), cwd=source, encoding="utf-8",
                                stdout=subprocess.PIPE, stderr=subprocess.STDOUT)
        print(result.stdout, end="", flush=True)
        assert result.returncode == status, (arguments, result.returncode, status)
        return result.stdout

    assert "argument with spaces" in call("run", "--", "printf", "%s\\n", "argument with spaces")
    assert "run requires a command" in call("run", "--", status=1)
    original = config.read_text()
    config.write_text(original + '\nwrappper = "invalid"\n')
    try:
        assert "unknown project settings" in call("run", "--", "true", status=1)
    finally:
        config.write_text(original)
    call("run", "--", "sh", "-c", "exit 23", status=23)
    call("test")
    call("run", "--", "sh", "-c", "test ! -e /run/secrets")
    call("run", "--", "python3", "-c", "import socket; socket.create_connection(('192.0.2.1',9))", status=1)
    running = subprocess.Popen(client + ["run", "--", "python3", "-c",
                               "import signal; print('cancel-ready',flush=True); signal.pause()"],
                               cwd=source, encoding="utf-8", stdout=subprocess.PIPE, stderr=subprocess.STDOUT)
    run = None
    for line in running.stdout:
        print(line, end="", flush=True)
        if line.startswith("run="):
            run = line.split()[0].split("=", 1)[1]
        if line.strip() == "cancel-ready":
            break
    try:
        assert run is not None and running.poll() is None
        assert run.isdecimal()
        assert run in call("status")
        mode = subprocess.check_output(["ssh", sys.argv[1], "podman", "inspect", "--format",
                                        "{{.HostConfig.NetworkMode}}", "ci-buddy-" + run], encoding="utf-8")
        assert mode.strip() == "none", mode
    finally:
        if run is not None and running.poll() is None:
            call("cancel", run)
        running.communicate()
    assert running.returncode != 0
    call("cancel", run)
    probe = """import fcntl,sys
from pathlib import Path
root = Path.home() / '.ci-buddy' / 'projects' / 'example'
out = root / 'runs' / sys.argv[1]
"""
    def run_probe(action, run):
        subprocess.run(["ssh", sys.argv[1], "python3 -c " + shlex.quote(probe + action) + " " + run], check=True)
    run_probe("""slot = (out / 'slot').read_text().strip()
with (root / 'slots' / slot / 'lock').open('r+') as lock:
    fcntl.flock(lock,fcntl.LOCK_EX|fcntl.LOCK_NB)""", run)
    call("run", "--", "printf", "slot-released\\n")
    call("run", "--exclusive", "--", "sh", "-c", 'printf readable >"$CI_BUDDY_DATA/probe"')
    call("run", "--", "sh", "-c", '! printf wrong >"$CI_BUDDY_DATA/probe"; test "$(cat "$CI_BUDDY_DATA/probe")" = readable')
    commit = subprocess.check_output(["git", "rev-parse", "HEAD"], cwd=source, encoding="utf-8").strip()
    gate = call("gate")
    run = next(line.split()[0].split("=")[1] for line in gate.splitlines() if line.startswith("run="))
    verifying = subprocess.Popen(client + ["verify", commit], cwd=source, encoding="utf-8",
                                 stdout=subprocess.PIPE, stderr=subprocess.STDOUT)
    try:
        assert verifying.stdout.readline().startswith("waiting bench_task=")
        run_probe("assert not (out / 'bench-exit').exists()\n(out / 'release').write_text('release')", run)
        report = verifying.communicate()[0]
    finally:
        if verifying.poll() is None:
            call("cancel", run)
            verifying.communicate()
    assert verifying.returncode == 0 and "gate green" in report and "bench green " + run in report, report
    config.write_text("\n".join('gate = "false"' if line.startswith("gate = ") else line for line in original.splitlines()) + "\n")
    subprocess.run(["git", "-c", "user.name=example", "-c", "user.email=example@example.invalid",
                    "commit", "-qam", "red gate"], cwd=source, check=True)
    red = call("gate", status=1)
    run = next(line.split()[0].split("=")[1] for line in red.splitlines() if line.startswith("run="))
    assert "bench_task=" not in red, red
    run_probe("assert not (out / 'bench-task').exists()", run)
    commit = subprocess.check_output(["git", "rev-parse", "HEAD"], cwd=source, encoding="utf-8").strip()
    call("verify", commit, status=1)
print("example end-to-end: green")
