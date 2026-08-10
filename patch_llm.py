#!/usr/bin/env python3
"""One-shot patch for captcha_solver.py:

1. Insert a module-level FEWSHOT_POOL (extended with hCaptcha trivia
   question types) plus build_llm_prompt()/clean_llm_answer() helpers right
   before `async def _dump_clickables` so the test harness can exercise the
   EXACT same production prompt path.
2. Replace the nested `_FEWSHOT_POOL = [...]` list inside the accessibility
   solver with an alias to the module-level pool, so production uses the
   extended pool too.

Mirrors the fix_captcha.py pattern already used in this repo.
"""

path = "captcha_solver.py"
text = open(path).read()


def replace_once(old: str, new: str, label: str):
    global text
    n = text.count(old)
    if n != 1:
        raise SystemExit(f"FAIL {label}: expected exactly 1 occurrence, found {n}")
    text = text.replace(old, new)
    print(f"OK  {label}")


# ── 1. Module-level FEWSHOT_POOL + prompt helpers ─────────────────────────
ANCHOR = "async def _dump_clickables(page, frame, iframe_box, log):"

module_block = '''# ═══════════════════════════════════════════════════════════════
# LLM few-shot prompt (module-level so the test harness exercises the
# EXACT same production prompt path: pool selection + think:false call +
# answer cleaning).
# ═══════════════════════════════════════════════════════════════

# Few-shot examples injected into the LLM prompt. Small local models
# (llama3.2:1b, qwen3:1.7b) only answer CAPTCHA trivia correctly when they
# are shown the same question TYPE first, so pick the most relevant examples
# for each question before asking.
FEWSHOT_POOL = [
    ("You start with 6 coins in a jar. On Wednesday, you put 9 coins into the jar. How many coins are in your jar now?", "15"),
    ("Your coin jar has 8 coins. On Monday, you add 9 coins. How many coins are in the jar?", "17"),
    ("What vegetable is white inside and brown outside?", "potato"),
    ("What direction does the sun set in?", "west"),
    ("What liquid do you use to wash your body?", "soap"),
    ("What do we call a container that holds coins?", "jar"),
    ("What is the capital of France?", "paris"),
    ("How many days are in a week?", "7"),
    ("What color is the sky?", "blue"),
    ("What do bees make?", "honey"),
    ("What is the largest planet in our solar system?", "jupiter"),
    ("How many legs does a spider have?", "8"),
    ("What do we call a baby dog?", "puppy"),
    ("What is the opposite of hot?", "cold"),
    ("What gas do plants absorb?", "carbon dioxide"),
    ("What instrument has 88 keys?", "piano"),
    ("What do you use to cut paper?", "scissors"),
    ("How many minutes are in an hour?", "60"),
    ("What animal says moo?", "cow"),
    ("What is the tallest land animal?", "giraffe"),
    ("What do you call the person who flies a plane?", "pilot"),
    ("What color do you get when you mix red and white?", "pink"),
    ("How many wheels does a car have?", "4"),
    ("What is the first month of the year?", "january"),
    ("What do you use to write on a blackboard?", "chalk"),
    ("What is the freezing point of water in celsius?", "0"),
    ("Can bread be stored frozen?", "yes"),
    ("Do cats meow?", "yes"),
    # ── hCaptcha trivia types (added from the AI accuracy sweep) ──
    ("Which country has the capital city Tokyo?", "japan"),
    ("What is the capital of Kenya?", "nairobi"),
    ("What is the capital of Nigeria?", "abuja"),
    ("What is the capital of South Africa?", "pretoria"),
    ("What is the largest mammal in the world?", "blue whale"),
    ("What is the national sport of Japan?", "sumo"),
    ("What is the smallest bird in the world?", "hummingbird"),
    ("What is the largest bird in the world?", "ostrich"),
    ("What is the tallest building in the world?", "burj khalifa"),
    ("What is the largest land carnivore?", "polar bear"),
    ("What is the largest rainforest in the world?", "amazon"),
    ("What is the highest waterfall in the world?", "angel falls"),
    ("What is the driest place on earth?", "atacama"),
    ("How many letters are in the word elephant?", "8"),
    ("What is the longest river in the world?", "nile"),
]


def build_llm_prompt(question: str) -> str:
    """Pick the 4 most relevant few-shot examples and build the prompt."""
    stop = {"what", "which", "how", "many", "much", "does", "do", "is", "are",
            "the", "and", "with", "your", "you", "that", "this", "from", "into",
            "there", "have", "has", "can", "would", "about", "when", "where",
            "its", "answer", "question", "following", "single", "word", "number",
            "phrase", "please", "put", "add", "call", "calls", "one", "using"}
    qw = {w for w in re.findall(r"[a-z]{3,}", question.lower()) if w not in stop}
    scored = []
    for eq, ea in FEWSHOT_POOL:
        ew = {w for w in re.findall(r"[a-z]{3,}", eq.lower())}
        scored.append((len(qw & ew), eq, ea))
    scored.sort(key=lambda x: -x[0])
    lines = ["Answer each question with exactly ONE word or number.",
             "No punctuation, no explanation, no quotes."]
    for _score, eq, ea in scored[:4]:
        lines.append("Question: " + eq)
        lines.append("Answer: " + ea)
    lines.append("Question: " + question)
    lines.append("Answer:")
    return "\\n".join(lines)


def clean_llm_answer(raw: str) -> str:
    """Normalize an LLM answer: lowercase, strip punctuation and
    rambling preambles, keep up to 3 words (captcha answers can be
    phrases like 'dog food' or 'living room'). Returns '' if empty."""
    if not raw:
        return ""
    # Lowercase, drop quotes/brackets/periods but keep word separators
    s = re.sub(r"[\\"'`\\[\\](){}<>]", "", raw)
    s = s.replace(".", " ").replace(",", " ").replace(";", " ").replace(":", " ")
    s = s.replace("\\n", " ").replace("\\t", " ").replace("-", " ")
    s = s.lower()
    # Strip rambling preambles repeatedly so the answer word survives:
    # "i think the answer is X", "it is X", "probably X", "my answer is X"
    _preamble = re.compile(
        r'^(?:(?:i\\s+(?:think|believe|guess|would\\s+say|am\\s+pretty\\s+sure))'
        r'|(?:the\\s+answer\\s+(?:is|would\\s+be))'
        r'|(?:the\\s+(?:correct|right)\\s+answer\\s+(?:is|would\\s+be))'
        r'|(?:my\\s+answer\\s+is)'
        r'|(?:that\\s+would\\s+be)'
        r'|(?:it\\s+is)'
        r'|(?:it\\'?s)'
        r'|(?:the\\s+word\\s+is)'
        r'|(?:this\\s+is)'
        r'|(?:probably|maybe|likely|definitely|obviously))'
        r'\\b[\\s,:;-]*')
    for _ in range(3):
        if not s:
            break
        s2 = _preamble.sub('', s)
        if s2 == s:
            break
        s = s2
    words = [w for w in s.split() if re.search(r"[a-z0-9]", w)]
    if not words:
        return ""
    # Drop filler words that sometimes leak out
    stop = {"the", "a", "an", "is", "are", "it", "of", "to", "in", "for",
            "answer", "with", "and", "or", "be", "please",
            "i", "think", "believe", "guess", "probably", "maybe", "likely",
            "would", "should", "could", "that", "this", "its", "correct", "right",
            "my", "so", "just", "really", "very", "most"}
    cleaned = [w for w in words if w not in stop]
    if not cleaned:
        return ""
    return " ".join(cleaned[:3])


'''
replace_once(ANCHOR, module_block + ANCHOR, "insert module-level LLM prompt helpers")

# ── 2. Nested pool inside the accessibility solver → alias ────────────────
NESTED_POOL = '''    # Few-shot examples injected into the LLM prompt. Small local models
    # (llama3.2:1b etc.) only answer CAPTCHA trivia correctly when they are
    # shown the same question TYPE first, so pick the most relevant examples
    # for each question before asking.
    _FEWSHOT_POOL = [
        ("You start with 6 coins in a jar. On Wednesday, you put 9 coins into the jar. How many coins are in your jar now?", "15"),
        ("Your coin jar has 8 coins. On Monday, you add 9 coins. How many coins are in the jar?", "17"),
        ("What vegetable is white inside and brown outside?", "potato"),
        ("What direction does the sun set in?", "west"),
        ("What liquid do you use to wash your body?", "soap"),
        ("What do we call a container that holds coins?", "jar"),
        ("What is the capital of France?", "paris"),
        ("How many days are in a week?", "7"),
        ("What color is the sky?", "blue"),
        ("What do bees make?", "honey"),
        ("What is the largest planet in our solar system?", "jupiter"),
        ("How many legs does a spider have?", "8"),
        ("What do we call a baby dog?", "puppy"),
        ("What is the opposite of hot?", "cold"),
        ("What gas do plants absorb?", "carbon dioxide"),
        ("What instrument has 88 keys?", "piano"),
        ("What do you use to cut paper?", "scissors"),
        ("How many minutes are in an hour?", "60"),
        ("What animal says moo?", "cow"),
        ("What is the tallest land animal?", "giraffe"),
        ("What do you call the person who flies a plane?", "pilot"),
        ("What color do you get when you mix red and white?", "pink"),
        ("How many wheels does a car have?", "4"),
        ("What is the first month of the year?", "january"),
        ("What do you use to write on a blackboard?", "chalk"),
        ("What is the freezing point of water in celsius?", "0"),
        ("Can bread be stored frozen?", "yes"),
        ("Do cats meow?", "yes"),
    ]'''

replace_once(
    NESTED_POOL,
    '''    # Few-shot pool lives at module level (FEWSHOT_POOL) so the test
    # harness exercises the exact same production prompt path.
    _FEWSHOT_POOL = FEWSHOT_POOL''',
    "nested _FEWSHOT_POOL -> module-level alias",
)

open(path, "w").write(text)
print("DONE")
