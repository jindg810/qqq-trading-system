import subprocess, time, os

CMDS = [
    ["python", "live_trader.py"],
    ["python", "trader_web.py"]
]

procs = [subprocess.Popen(c) for c in CMDS]

while True:
    for i, p in enumerate(procs):
        if p.poll() is not None:
            procs[i] = subprocess.Popen(CMDS[i])
    time.sleep(5)