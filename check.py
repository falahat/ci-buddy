from pathlib import Path
import ipaddress
import re
import subprocess

root = Path(__file__).resolve().parent
names = subprocess.check_output(["git", "ls-files", "--cached", "--others", "--exclude-standard", "-z"], cwd=root).decode().split("\0")
allowed = ("/work", "/run/secrets", "/sys/fs/cgroup", "/tmp")
absolute = re.compile(r"(?<![A-Za-z0-9_.$})/~])/(?:[A-Za-z0-9_.-]+/?)+")
drive = re.compile(r"\b[A-Za-z]:[" + re.escape(chr(92)) + "/]")
unc = re.compile(re.escape(chr(92) * 2) + r"[A-Za-z0-9_.-]+" + re.escape(chr(92)))
code_lines = example_lines = 0
for name in filter(None, names):
    lines = (root / name).read_text().splitlines()
    if name.startswith("example/") or name == "e2e.py":
        example_lines += len(lines)
    elif Path(name).suffix in (".sh", ".py") or name == "ci-buddy":
        code_lines += len(lines)
    for number, line in enumerate(lines, 1):
        line = re.sub(r"\b[a-z][a-z0-9+.-]*://\S+", "", line)
        # These single-character Bash substitutions cannot name a root.
        line = re.sub(r"\$\{[A-Za-z_][A-Za-z0-9_]*//[-_]/[-_]\}", "", line)
        if name == 'worker/startup.sh':
            for value in re.findall(r'[0-9a-fA-F:.]+/\d+', line):
                try:
                    ipaddress.ip_network(value)
                except ValueError:
                    continue
                line = line.replace(value, '')
        assert not drive.search(line) and not unc.search(line), (name, number, "absolute Windows path")
        for match in absolute.finditer(line):
            # Documentation's root placeholder names a caller-supplied path.
            if line[:match.start()].endswith("<root>"):
                continue
            path = match.group()
            native = ('/etc/os-release', '/etc/ssh', '/etc/apt/sources.list.d', '/usr/share/keyrings', '/dev/ttyS1', '/usr/sbin/sshd', '/bin/bash', '/dev/null')
            prefixes = allowed + (native if name in ('worker/startup.sh', 'check.py') else ())
            if name == 'install-client.sh':
                prefixes += ('/dev/null',)
            assert any(path == prefix or path.startswith(prefix + "/") for prefix in prefixes), (name, number, path)
assert code_lines < 2400, code_lines
assert example_lines < 350, example_lines
print(f"code_lines={code_lines} example_lines={example_lines}")
