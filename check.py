from pathlib import Path
import re
import subprocess

root = Path(__file__).resolve().parent
names = subprocess.check_output(["git", "ls-files", "--cached", "--others", "--exclude-standard", "-z"], cwd=root).decode().split("\0")
allowed = ("/work", "/run/secrets", "/sys/fs/cgroup", "/tmp")
absolute = re.compile(r"(?<![A-Za-z0-9_.$})/~])/(?:[A-Za-z0-9_.-]+/?)+")
drive = re.compile(r"\b[A-Za-z]:[" + re.escape(chr(92)) + "/]")
unc = re.compile(re.escape(chr(92) * 2) + r"[A-Za-z0-9_.-]+" + re.escape(chr(92)))
tool_lines = example_lines = 0
for name in filter(None, names):
    lines = (root / name).read_text().splitlines()
    if name.startswith("example/") or name == "e2e.py":
        example_lines += len(lines)
    else:
        tool_lines += len(lines)
    for number, line in enumerate(lines, 1):
        line = re.sub(r"\b[a-z][a-z0-9+.-]*://\S+", "", line)
        assert not drive.search(line) and not unc.search(line), (name, number, "absolute Windows path")
        for path in absolute.findall(line):
            assert any(path == prefix or path.startswith(prefix + "/") for prefix in allowed), (name, number, path)
assert tool_lines < 600, tool_lines
assert example_lines < 150, example_lines
print(f"tool_lines={tool_lines} example_lines={example_lines}")
