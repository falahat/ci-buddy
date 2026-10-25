import os
from pathlib import Path
import socket
import sys

out = Path(os.environ["CI_BUDDY_OUT"])
if sys.argv[1] == "prepare":
    assert not Path("/run/secrets").exists()
    assert any(name != "lo" for _, name in socket.if_nameindex()), "prepare has no network interface"
    (out / "prepare.txt").write_text("prepare network, no secrets\n")
elif sys.argv[1] == "gate":
    os.mkfifo(out / "release", 0o600)
    (out / "report.txt").write_text("gate green\n")
else:
    with (out / "release").open() as release:
        release.read()
    with (out / "report.txt").open("a") as report:
        report.write("bench green " + os.environ["CI_BUDDY_RUN"] + "\n")
