import datetime
import os
from pathlib import Path
import stat
import shutil
import subprocess
import sys
import tarfile
import tempfile
import safe_output

out = Path(sys.argv[1])
if sys.argv[2:] == ["log"]:
    saved = next((out.parents[1] / "logs").glob("*/" + out.name + ".tar.zst"))
    with subprocess.Popen(["zstd", "-d", "-q", "-c", str(saved)], stdout=subprocess.PIPE) as process:
        with tarfile.open(fileobj=process.stdout, mode="r|") as archive:
            for entry in archive:
                if entry.isfile() and entry.name.endswith("runner.log"):
                    safe_output.stream(archive.extractfile(entry), sys.stdout)
        if process.wait():
            raise subprocess.CalledProcessError(process.returncode, process.args)
    sys.exit()
month = datetime.datetime.now(datetime.timezone.utc).strftime("%Y-%m")
destination = out.parent.parent / "logs" / month / (out.name + ".tar.zst")
destination.parent.mkdir(parents=True, exist_ok=True)
descriptor, temporary_name = tempfile.mkstemp(dir=destination.parent, prefix=out.name + ".")
temporary = Path(temporary_name)
logs = []
try:
    with os.fdopen(descriptor, "wb") as compressed:
        process = subprocess.Popen(["zstd", "-3", "-q", "-c"], stdin=subprocess.PIPE, stdout=compressed)
        try:
            with tarfile.open(fileobj=process.stdin, mode="w|") as archive:
                for path in sorted(out.rglob("*")):
                    mode = path.lstat().st_mode
                    if not stat.S_ISREG(mode) or mode & 0o111:
                        continue
                    # Never dereference a job's link or store its copied executable.
                    with os.fdopen(os.open(path, os.O_RDONLY | os.O_NOFOLLOW | os.O_NONBLOCK), "rb") as source:
                        info = os.fstat(source.fileno())
                        if not stat.S_ISREG(info.st_mode):
                            raise ValueError(f"not a regular file: {path}")
                        entry = archive.gettarinfo(arcname=path.relative_to(out).as_posix(), fileobj=source)
                        archive.addfile(entry, source)
                    if path.suffix in (".log", ".jsonl") or path.name == "junit.xml":
                        logs.append(path)
        finally:
            process.stdin.close()
            status = process.wait()
        if status:
            raise subprocess.CalledProcessError(status, process.args)
    temporary.replace(destination)
    print(f"log_bytes={destination.stat().st_size}")
    for path in logs:
        path.unlink()
finally:
    temporary.unlink(missing_ok=True)
