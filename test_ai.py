#!/usr/bin/env python3
"""Test the solver's AI path (qwen3:1.7b Ollama) on questions the LOCAL KB
cannot answer.

Mirrors the exact production flow in captcha_solver.py:
  1. Local KB first  → _solve_knowledge_question + _solve_semantic
  2. LLM fallback    → build_llm_prompt() few-shot prompt, think:false,
                       temperature 0.2, stop ["\n","."], num_predict 16,
                       then clean_llm_answer() post-processing.

Loop (per user request): ask → check response → fix → ask again with fresh
sampling until the question is answered correctly.

Questions marked KB-known (e.g. 'How many seconds in a hour' → 3600) are
still asked anyway per the user's explicit request — the pre-flight only
warns when the local KB already covers a question.
"""

import json
import sys
import time
from urllib.request import Request, urlopen
from urllib.error import URLError

import captcha_solver as cs

OLLAMA_URL = "https://ollama-production-c7dd.up.railway.app"  # qwen3:1.7b
MODEL = "qwen3:1.7b"

# ── Questions verified as KB-UNKNOWN (local solver cannot answer them) ──
# Format: (question, expected_answer_keywords)
# The last 3 are user-requested additions (2 of them the KB actually knows
# — '3600' and sloth/snail — but the user asked to test the AI on them anyway).
TEST_QUESTIONS = [
    ("Which country has the capital city Tokyo?", ["japan"]),
    ("What is the largest mammal in the world?", ["blue whale", "whale"]),
    ("What is the national sport of Japan?", ["sumo"]),
    ("What is the capital of Nigeria?", ["abuja"]),
    ("What is the capital of South Africa?", ["pretoria"]),
    ("What is the smallest bird in the world?", ["hummingbird"]),
    ("What is the largest bird in the world?", ["ostrich"]),
    ("What is the tallest building in the world?", ["burj khalifa", "khalifa"]),
    ("What is the largest land carnivore?", ["polar bear"]),
    ("How many letters are in the word elephant?", ["8", "eight"]),
    # ── User-requested extras ──
    ("How many seconds in a hour", ["3600"]),
    ("Slowest animal", ["sloth", "snail"]),
    ("Who's the richest person on earth", ["elon musk", "musk", "bezos", "arnault"]),
]


def kb_answer(question: str):
    """Local KB path (exactly what production runs before the LLM)."""
    ans = cs._solve_knowledge_question(question)
    if ans is not None:
        return ans
    return cs._solve_semantic(question)


def query_ollama(question: str, temperature: float = 0.2, timeout: float = 60.0) -> str:
    """Exact production payload from captcha_solver._ollama_answer_text."""
    payload = {
        "model": MODEL,
        "stream": False,
        "keep_alive": "30m",
        "think": False,
        "options": {"temperature": temperature, "num_predict": 16,
                    "stop": ["\n", "."]},
        "messages": [{"role": "user", "content": cs.build_llm_prompt(question)}],
    }
    data = json.dumps(payload).encode()
    try:
        req = Request(f"{OLLAMA_URL}/api/chat", data=data,
                      headers={"Content-Type": "application/json"})
        resp = urlopen(req, timeout=timeout)
        result = json.loads(resp.read().decode())
        raw = result.get("message", {}).get("content", "")
        return cs.clean_llm_answer(raw)
    except Exception as e:
        print(f"    ⚠ request error: {e}")
        return ""


def check_answer(response: str, keywords) -> bool:
    r = (response or "").lower().strip()
    return any(k.lower() in r for k in keywords)


def main():
    print("=" * 66)
    print(f"AI test: {MODEL} @ {OLLAMA_URL}")
    print("Path: local KB first → LLM fallback (production prompt)")
    print("=" * 66)

    # ── Warm-up: wake the model ──
    print("\n[Warm-up] ...")
    for _ in range(3):
        if query_ollama("Say hello", timeout=120):
            print("  model warm ✓")
            break
        time.sleep(20)
    else:
        print("  warm-up FAILED — model unreachable")
        sys.exit(1)

    # ── Pre-flight: report KB coverage ──
    # KB-known questions are still asked per the user's explicit request —
    # the KB answer is just reported so we know production wouldn't use the AI.
    print("\n[Pre-flight] KB coverage check:")
    for q, kw in TEST_QUESTIONS:
        kb = kb_answer(q)
        status = "⚠ KB knows it" if kb else "OK (KB-unknown)"
        print(f"  {status:22} {q[:60]}")
        if kb:
            print(f"    -> local KB answers '{kb}' (production skips the AI here) — asking anyway")

    # ── Ask → check → fix → fresh-ask loop ──
    print("\n" + "=" * 66)
    print("Running questions...")
    print("=" * 66)

    MAX_FRESH = 5  # fresh attempts with varied sampling before giving up
    correct = 0
    total = len(TEST_QUESTIONS)
    failures = []

    for i, (q, kw) in enumerate(TEST_QUESTIONS, 1):
        print(f"\n[Q{i}/{total}] {q}")
        ok = False
        last_resp = ""
        for attempt in range(MAX_FRESH):
            # temperature ladder: 0.2 (production) → 0.6 → 1.0 → 0.4 → 1.4
            temp = [0.2, 0.6, 1.0, 0.4, 1.4][attempt]
            start = time.time()
            resp = query_ollama(q, temperature=temp)
            elapsed = time.time() - start
            last_resp = resp
            if not resp:
                print(f"  attempt {attempt+1} (t={temp}): ❌ no response ({elapsed:.0f}s)")
                continue
            ok = check_answer(resp, kw)
            mark = "✅" if ok else "❌"
            print(f"  attempt {attempt+1} (t={temp}): {mark} '{resp}' ({elapsed:.0f}s)")
            if ok:
                break
        if ok:
            correct += 1
        else:
            failures.append((q, kw, last_resp))

    print("\n" + "=" * 66)
    print(f"SCORE: {correct}/{total} ({100 * correct / total:.0f}%)")
    print("=" * 66)

    if failures:
        print("\nFAILURES TO FIX:")
        for q, kw, resp in failures:
            print(f"  Q: {q}")
            print(f"  Expected: {kw}")
            print(f"  Got:      '{resp}'")
            print()
    return correct, total, failures


if __name__ == "__main__":
    correct, total, failures = main()
    sys.exit(1 if failures else 0)
