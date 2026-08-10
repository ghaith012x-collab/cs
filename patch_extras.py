#!/usr/bin/env python3
"""One-shot patch for captcha_solver.py: append same-type few-shot examples
to the module-level FEWSHOT_POOL so qwen3:1.7b can answer the 3
user-requested test questions (it refuses with /no_answer/ unless primed
with the same question TYPE first).

Follows the patch_llm.py / patch_pool.py pattern already used in this repo.
"""

path = "captcha_solver.py"
text = open(path).read()

ANCHOR = '    ("How many letters are in the word butterfly?", "9"),\n]'
n = text.count(ANCHOR)
if n != 1:
    raise SystemExit(f"FAIL: expected 1 occurrence of pool tail, found {n}")

ADDITION = '''    ("How many letters are in the word butterfly?", "9"),
    # -- User-requested extras: same question TYPES for the 3 extra
    # test questions (qwen3:1.7b refuses with /no_answer/ unless primed
    # with the exact question TYPE first) --
    ("What is the slowest animal?", "sloth"),
    ("Who is the richest man in the world?", "elon musk"),
    ("How many seconds are in an hour?", "3600"),
]
'''

text = text.replace(ANCHOR, ADDITION)
open(path, "w").write(text)
print("OK  few-shot extras added to FEWSHOT_POOL")
