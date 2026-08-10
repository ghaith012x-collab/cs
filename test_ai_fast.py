#!/usr/bin/env python3
"""Resumable, chunked AI test runner.

The full ladder test (13 questions x up to 5 attempts) exceeds the sandbox's
180s command cap, so this runner:

  * warms the model up,
  * works through test_ai.TEST_QUESTIONS one at a time (temp ladder),
  * persists every result to data/ai_test_results.jsonl,
  * skips questions already answered on a re-run.

Usage:
    python3 test_ai_fast.py [--budget 150] [--fresh]
"""

import argparse
import json
import os
import time

import test_ai as t

RESULTS = "data/ai_test_results.jsonl"
MODEL = t.MODEL


def load_done() -> dict:
    done = {}
    if os.path.exists(RESULTS):
        for line in open(RESULTS, encoding="utf-8"):
            line = line.strip()
            if not line:
                continue
            try:
                row = json.loads(line)
                done[row["question"]] = row
            except Exception:
                pass
    return done


def save(row: dict) -> None:
    with open(RESULTS, "a", encoding="utf-8") as f:
        f.write(json.dumps(row) + "\n")


def main() -> int:
    ap = argparse.ArgumentParser()
    ap.add_argument("--budget", type=int, default=150, help="wall-clock budget in seconds")
    ap.add_argument("--fresh", action="store_true", help="delete old results first")
    args = ap.parse_args()

    if args.fresh and os.path.exists(RESULTS):
        os.remove(RESULTS)

    done = load_done()
    print(f"[runner] model={MODEL}  already-tested={len(done)}/{len(t.TEST_QUESTIONS)}")
    print(f"[runner] wall budget={args.budget}s — will stop mid-suite and resume next run")
    print("=" * 66)

    deadline = time.time() + args.budget

    # ── warm-up ────────────────────────────────────────────────────────────
    if not done:
        print("[warm-up] ...")
        warm = False
        for _ in range(3):
            if time.time() > deadline - 10:
                print("  budget exhausted before warm-up; run again")
                return 2
            r = t.query_ollama("Say hello", timeout=80)
            if r:
                print(f"  warm OK: '{r}'")
                warm = True
                break
            time.sleep(5)
        if not warm:
            print("  WARM-UP FAILED — model unreachable")
            return 1

    # ── per-question loop ──────────────────────────────────────────────────
    remaining = [q for q, _ in t.TEST_QUESTIONS if q not in done]
    for i, (q, kw) in enumerate(t.TEST_QUESTIONS, 1):
        if q in done:
            continue
        if time.time() > deadline - 5:
            print(f"\n[runner] budget hit — {len(remaining)} questions left to test, re-run me")
            break
        print(f"\n[Q{i}/{len(t.TEST_QUESTIONS)}] {q}")
        attempts = []
        ok = False
        for attempt in range(5):
            temp = [0.2, 0.6, 1.0, 0.4, 1.4][attempt]
            st = time.time()
            resp = t.query_ollama(q, temperature=temp)
            el = time.time() - st
            a_ok = bool(resp) and t.check_answer(resp, kw)
            attempts.append({"temp": temp, "response": resp, "correct": a_ok,
                             "elapsed": round(el, 1)})
            mark = "OK" if a_ok else ("-" if not resp else "X")
            print(f"  t={temp}: {mark} '{resp}' ({el:.0f}s)")
            if a_ok:
                ok = True
                break
        row = {"question": q, "keywords": kw, "correct": ok, "attempts": attempts,
               "tested_at": time.time()}
        save(row)
        print(f"  -> {'CORRECT' if ok else 'FAILED'}")
        remaining.remove(q)

    # ── summary ────────────────────────────────────────────────────────────
    done = load_done()
    total = len(t.TEST_QUESTIONS)
    correct = sum(1 for r in done.values() if r["correct"])
    print("\n" + "=" * 66)
    print(f"PROGRESS: {len(done)}/{total} tested, {correct} correct "
          f"({100 * correct / max(len(done), 1):.0f}% of tested)")
    print("=" * 66)
    if len(done) >= total:
        print("SUITE COMPLETE — run test_ai_fast_report.py for the full report")
    else:
        print("NOT FINISHED — re-run `python3 test_ai_fast.py` to continue")
    return 0 if len(done) >= total and correct == total else 2


if __name__ == "__main__":
    sys_exit = main()
    raise SystemExit(sys_exit)
