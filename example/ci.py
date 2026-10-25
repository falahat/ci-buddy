import os
from pathlib import Path
import sys

out = Path(os.environ["CI_BUDDY_OUT"])
if sys.argv[1] == "gate":
    os.mkfifo(out / "release", 0o600)
    (out / "report.txt").write_text("gate green\n")
else:
    with (out / "release").open() as release:
        release.read()
    with (out / "report.txt").open("a") as report:
        report.write("bench green " + os.environ["CI_BUDDY_RUN"] + "\n")
