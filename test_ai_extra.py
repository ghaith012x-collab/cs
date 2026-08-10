#!/usr/bin/env python3
"""Focused run: the 3 user-requested extra questions through the exact
test_ai harness path (production prompt, think:false, temp ladder)."""

import sys
import time

import test_ai as t

print("warm-up...")
warm = False
for _ in range(3):
    r = t.query_ollama("Say hello", timeout=90)
    if r:
        print("  warm OK:", r)
        warm = True
        break
    time.sleep(15)
if not warm:
    print("WARM-UP FAILED")
    sys.exit(1)

qs = t.TEST_QUESTIONS[-3:]
for i, (q, kw) in enumerate(qs, 1):
    print(f"\n[Q{i}/3] {q}")
    ok = False
    last = ""
    for attempt in range(5):
        temp = [0.2, 0.6, 1.0, 0.4, 1.4][attempt]
        st = time.time()
        resp = t.query_ollama(q, temperature=temp)
        el = time.time() - st
        last = resp
        if not resp:
            print(f"  attempt {attempt+1} (t={temp}): empty ({el:.0f}s)")
            continue
        ok = t.check_answer(resp, kw)
        print(f"  attempt {attempt+1} (t={temp}): {'OK' if ok else 'X'} '{resp}' ({el:.0f}s)")
        if ok:
            break
    print("  -> CORRECT" if ok else f"  -> FAILED, last: '{last}'")
