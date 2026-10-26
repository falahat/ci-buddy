from pathlib import Path
import re
import ipaddress
import subprocess

root = Path(__file__).resolve().parent
names = subprocess.check_output(["git", "ls-files", "--cached", "--others", "--exclude-standard", "-z"], cwd=root).decode().split("\0")
allowed = ("/work", "/run/secrets", "/sys/fs/cgroup", "/tmp")
absolute = re.compile(r"(?<![A-Za-z0-9_.$})/~])/(?:[A-Za-z0-9_.-]+/?)+")
drive = re.compile(r"\b[A-Za-z]:[" + re.escape(chr(92)) + "/]")
unc = re.compile(re.escape(chr(92) * 2) + r"[A-Za-z0-9_.-]+" + re.escape(chr(92)))
code_lines = example_lines = 0
invalid_paths = []
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
        # Native network CIDRs and provider API resources are not filesystem paths.
        if name.startswith('worker/') or name == 'test_enrollment.py':
            for candidate in re.findall(r'[0-9a-fA-F:.]+/\d+', line):
                try:
                    ipaddress.ip_network(candidate)
                except ValueError:
                    continue
                line = line.replace(candidate, '')
        assert not drive.search(line) and not unc.search(line), (name, number, "absolute Windows path")
        for match in absolute.finditer(line):
            # Documentation's root placeholder names a caller-supplied path.
            if line[:match.start()].endswith("<root>"):
                continue
            path = match.group()
            prefixes = allowed
            if name == 'worker/bootstrap.sh':
                prefixes += ('/etc', '/usr', '/run', '/var', '/bin', '/dev/null')
            if name == 'install-client.sh':
                prefixes += ('/dev/null',)
            if name in ('worker/policy.py', 'worker/enroll.py', 'worker/profile.py', 'test_enrollment.py', 'check.py'):
                prefixes += ('/tailnet', '/device', '/devices', '/keys', '/acl', '/repos', '/meta', '/routes', '/subnetworks')
            if name == 'check.py':
                prefixes += ('/etc', '/usr', '/run', '/var', '/bin', '/dev/null')
            if not any(path == prefix or path.startswith(prefix + "/") for prefix in prefixes):
                invalid_paths.append((name, number, path))
assert not invalid_paths, invalid_paths
if code_lines >= 1200:
    print(f"code budget exceeded: {code_lines}/1200; report size under the completion exception")
assert example_lines < 350, example_lines
print(f"code_lines={code_lines} example_lines={example_lines}")
