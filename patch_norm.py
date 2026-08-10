#!/usr/bin/env python3
"""One-shot patch for captcha_solver.py:

1. Insert a module-level _normalize_llm_question() helper right before
   build_llm_prompt().
2. Call it at the top of the module-level build_llm_prompt() (used by the
   AI test harness).
3. Call it at the top of the nested _build_llm_prompt() (production path)
   so both stay in sync.

Why: qwen3:1.7b emits /NoAnswer/ refusals for terse/contracted fragments
('Slowest animal', "Who's the richest person on earth") but answers
correctly when asked a full sentence ending in '?' ('What is the slowest
animal?' -> sloth, 'Who is the richest person on earth?' -> elon musk).
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


HELPER = '''def _normalize_llm_question(q: str) -> str:
    """Turn terse/contracted hCaptcha fragments into full questions so small
    models (qwen3:1.7b) answer instead of emitting /NoAnswer/ refusals.
    Examples:
      'Slowest animal'                  -> 'What is the slowest animal?'
      "Who's the richest person on earth" -> 'Who is the richest person on earth?'
    """
    s = (q or "").strip()
    if not s:
        return q or ""
    # Expand common contractions (small models misread "who's" as a name/refusal).
    for a, b in (("who's", "who is"), ("what's", "what is"), ("how's", "how is"),
                 ("where's", "where is"), ("when's", "when is"), ("why's", "why is"),
                 ("it's", "it is"), ("there's", "there is"), ("that's", "that is"),
                 ("don't", "do not"), ("doesn't", "does not"), ("can't", "cannot")):
        s = re.sub(r"\\b" + re.escape(a) + r"\\b", b, s, flags=re.IGNORECASE)
    # Bare fragment (no question starter) -> "What is the ..."
    if not re.match(
        r"^(?:what|which|who|whom|whose|how|when|where|why|is|are|was|were|"
        r"do|does|did|can|could|would|should|may|might|has|have|had|shall|will)\\b",
        s, re.IGNORECASE):
        s = "What is the " + s.lower()
    # Ensure it reads as a question (trailing '?').
    s = re.sub(r"[?.!]+$", "", s).strip()
    return s + "?"


'''

replace_once(
    "def build_llm_prompt(question: str) -> str:",
    HELPER + "def build_llm_prompt(question: str) -> str:",
    "insert _normalize_llm_question helper before build_llm_prompt",
)

replace_once(
    'def build_llm_prompt(question: str) -> str:\n'
    '    """Pick the 4 most relevant few-shot examples and build the prompt."""\n'
    "    stop =",
    'def build_llm_prompt(question: str) -> str:\n'
    '    """Pick the 4 most relevant few-shot examples and build the prompt."""\n'
    "    question = _normalize_llm_question(question)\n"
    "    stop =",
    "normalize in module-level build_llm_prompt",
)

replace_once(
    '    def _build_llm_prompt(question: str) -> str:\n'
    '        """Pick the 4 most relevant few-shot examples and build the prompt."""\n'
    "        stop =",
    '    def _build_llm_prompt(question: str) -> str:\n'
    '        """Pick the 4 most relevant few-shot examples and build the prompt."""\n'
    "        question = _normalize_llm_question(question)\n"
    "        stop =",
    "normalize in nested _build_llm_prompt (production)",
)

open(path, "w").write(text)
print("DONE")
