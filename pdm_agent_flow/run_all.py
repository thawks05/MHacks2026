"""Start the whole agent chain with one command:  python3 run_all.py

Starts, in order: supplier swarm -> Buyer -> Analyst -> Watcher, each as its own process, with
their logs interleaved and prefixed. Ctrl+C stops all of them.

Set SPACETIME_HOST / SPACETIME_DB first (see README). The Photon bridge runs separately on the
Mac signed into iMessage:  cd photon_bridge && npm start"""
import os
import subprocess
import sys
import threading
import time

AGENTS = [("suppliers", "suppliers_swarm.py"), ("buyer", "buyer_agent.py"),
          ("analyst", "analyst_agent.py"), ("watcher", "watcher_agent.py")]
HERE = os.path.dirname(os.path.abspath(__file__))


def _pipe(name: str, proc: subprocess.Popen):
    for line in proc.stdout:
        print(f"[{name:<9}] {line}", end="", flush=True)


def main():
    procs = []
    try:
        for name, script in AGENTS:
            p = subprocess.Popen([sys.executable, "-u", os.path.join(HERE, script)], cwd=HERE,
                                 stdout=subprocess.PIPE, stderr=subprocess.STDOUT, text=True)
            procs.append((name, p))
            threading.Thread(target=_pipe, args=(name, p), daemon=True).start()
            time.sleep(2)   # let each one bind its port before the next starts talking to it
        while all(p.poll() is None for _, p in procs):
            time.sleep(0.5)
        dead = [n for n, p in procs if p.poll() is not None]
        print(f"\n!! {', '.join(dead)} exited - stopping the rest. Scroll up for its error.")
    except KeyboardInterrupt:
        print("\nstopping...")
    finally:
        for _, p in procs:
            if p.poll() is None:
                p.terminate()
        for _, p in procs:
            try:
                p.wait(timeout=5)
            except subprocess.TimeoutExpired:
                p.kill()


if __name__ == "__main__":
    main()
