#!/usr/bin/env python3
"""One-shot patch for captcha_solver.py:

1. Improve few-shot example SELECTION in build_llm_prompt (module-level,
   harness) and _build_llm_prompt (nested, production):
   - +3 when the example opens with the same question word (what/who/how...)
   - +2 when both are superlative-style questions
   - -4 for arithmetic/coin examples unless the question itself is about coins
   Why: qwen3:1.7b emits /NoAnswer/ refusals when the few-shot mix spans
   unrelated domains (verified live: pilot+coin examples => refusal; a
   cluster of same-type 'richest/world' examples => 'elon musk').
2. Add two rich-person-type examples to FEWSHOT_POOL so the rich-type
   cluster is strong enough to fill the top-4 slots.
"""

path = "captcha_solver.py"
text = open(path).read()


def replace_once(old: str, new: str, label: str):
    global text
    n = text.count(old)
    if n != 1:
        raise SystemExit(f"FAIL {label}: expected exactly 1 occurrence, found {n}")
    text = text.replace(old, new, 1)
    print(f"OK  {label}")


MODULE_OLD = '''    scored = []
    for eq, ea in FEWSHOT_POOL:
        ew = {w for w in re.findall(r"[a-z]{3,}", eq.lower())}
        scored.append((len(qw & ew), eq, ea))
    scored.sort(key=lambda x: -x[0])'''

MODULE_NEW = '''    # Prefer examples of the same question TYPE: same opener word
    # (what/who/how/which), same superlative structure, and NEVER
    # arithmetic (coin) examples unless the question itself is about coins
    # — a 1.7B model refuses (/NoAnswer/) when the few-shot mix spans
    # unrelated domains (verified live).
    scored = []
    _q_first = (question.split() or [""])[0].lower()
    _q_superl = re.search(
        r"(largest|biggest|smallest|tallest|highest|longest|fastest|slowest|"
        r"richest|oldest|youngest|deepest|hottest|coldest|most|least)",
        question.lower())
    for eq, ea in FEWSHOT_POOL:
        ew = {w for w in re.findall(r"[a-z]{3,}", eq.lower())}
        score = len(qw & ew)
        if (eq.split() or [""])[0].lower() == _q_first:
            score += 3
        if _q_superl and re.search(
                r"(largest|biggest|smallest|tallest|highest|longest|fastest|"
                r"slowest|richest|oldest|youngest|deepest|hottest|coldest|"
                r"most|least)", eq.lower()):
            score += 2
        if "coin" in eq and "coin" not in question.lower():
            score -= 4
        scored.append((score, eq, ea))
    scored.sort(key=lambda x: -x[0])'''

replace_once(MODULE_OLD, MODULE_NEW, "scorer v2 (module build_llm_prompt)")

NESTED_OLD = '''        scored = []
        for eq, ea in _FEWSHOT_POOL:
            ew = {w for w in re.findall(r"[a-z]{3,}", eq.lower())}
            scored.append((len(qw & ew), eq, ea))
        scored.sort(key=lambda x: -x[0])'''

NESTED_NEW = '''        # Same-type preference (see module-level build_llm_prompt): opener
        # word + superlative bonus, arithmetic/coin penalty.
        scored = []
        _q_first = (question.split() or [""])[0].lower()
        _q_superl = re.search(
            r"(largest|biggest|smallest|tallest|highest|longest|fastest|"
            r"slowest|richest|oldest|youngest|deepest|hottest|coldest|most|"
            r"least)", question.lower())
        for eq, ea in _FEWSHOT_POOL:
            ew = {w for w in re.findall(r"[a-z]{3,}", eq.lower())}
            score = len(qw & ew)
            if (eq.split() or [""])[0].lower() == _q_first:
                score += 3
            if _q_superl and re.search(
                    r"(largest|biggest|smallest|tallest|highest|longest|"
                    r"fastest|slowest|richest|oldest|youngest|deepest|"
                    r"hottest|coldest|most|least)", eq.lower()):
                score += 2
            if "coin" in eq and "coin" not in question.lower():
                score -= 4
            scored.append((score, eq, ea))
        scored.sort(key=lambda x: -x[0])'''

replace_once(NESTED_OLD, NESTED_NEW, "scorer v2 (nested _build_llm_prompt)")

# ── Rich-person-type examples for the FEWSHOT_POOL ───────────────────
POOL_ANCHOR = '    ("How many seconds are in an hour?", "3600"),\n]'
if text.count(POOL_ANCHOR) != 1:
    raise SystemExit(f"FAIL pool anchor: expected 1, found {text.count(POOL_ANCHOR)}")
text = text.replace(POOL_ANCHOR, '''    ("How many seconds are in an hour?", "3600"),
    ("Who is the richest woman in the world?", "francoise bettencourt meyers"),
    ("Which person has the most money on earth?", "elon musk"),
]''', 1)
print("OK  rich-person-type examples added to FEWSHOT_POOL")

open(path, "w").write(text)
print("DONE")
