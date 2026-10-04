"""Checks every Store method against spacetimedb/src/index.ts:  python3 test_contract.py

For each method that writes, it records which reducer it calls and with how many arguments, then
checks that reducer exists in index.ts with exactly that many fields. Catches the bug where Python
and the database drift apart (it only shows up live otherwise, since test_flow.py uses a fake)."""
import inspect
import re
import sys

from contracts import PartHealth, OrderProposal
from store import Store

src = open("spacetimedb/src/index.ts").read()
reducers = {}
for m in re.finditer(r"export const (\w+) = spacetimedb\.reducer\(\s*\{(.*?)\}\s*,\s*\(", src, re.S):
    reducers[m.group(1)] = len(re.findall(r"\w+\s*:\s*t\.", m.group(2)))

calls = []
s = Store()
s.call = lambda name, *args: calls.append((name, len(args))) or None
s.rows = lambda q: [{"id": 1}]                     # so methods that read back an id don't crash
s.sql = lambda q: []

ph = PartHealth("drive_gear", 50.0, 10, 20, None, 1.0)
op = OrderProposal("drive_gear", "X", 1, 1.0, 1, "r", "needs_approval", 1.0, "")
samples = {"ph": ph, "o": op}

bad = 0
for name, fn in inspect.getmembers(Store, inspect.isfunction):
    if name.startswith("_") or name in ("call", "sql", "rows"):
        continue
    params = list(inspect.signature(fn).parameters.values())[1:]
    args = []
    for p in params:
        if p.default is not inspect.Parameter.empty:
            continue
        args.append(samples.get(p.name, 1 if p.annotation in (int, float) else "x"))
    calls.clear()
    try:
        getattr(s, name)(*args)
    except Exception as e:
        print(f"ERROR {name}: {e}"); bad += 1; continue
    for reducer, n in calls:
        if reducer not in reducers:
            print(f"FAIL  {name} -> {reducer}: no such reducer in index.ts"); bad += 1
        elif reducers[reducer] != n:
            print(f"FAIL  {name} -> {reducer}: sends {n} args, reducer takes {reducers[reducer]}"); bad += 1
        else:
            print(f"ok    {name} -> {reducer}({n})")

print("\nALL STORE CALLS MATCH index.ts" if not bad else f"\n{bad} PROBLEM(S)")
sys.exit(1 if bad else 0)
