#!/usr/bin/env python3
"""Replace the exact-question few-shot examples in the module-level
FEWSHOT_POOL with same-TYPE examples that use different content. The AI test
must measure real knowledge, not memorize leaked answers."""

path = "captcha_solver.py"
text = open(path).read()

OLD_BLOCK = '''    # ── hCaptcha trivia types (added from the AI accuracy sweep) ──
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
]'''

NEW_BLOCK = '''    # ── hCaptcha trivia question TYPES (content differs from the AI
    # sweep questions so the test measures real knowledge, not leakage) ──
    ("Which country has the capital city Beijing?", "china"),
    ("What is the capital of Kenya?", "nairobi"),
    ("What is the capital of Egypt?", "cairo"),
    ("What is the national sport of England?", "cricket"),
    ("What is the smallest mammal in the world?", "bumblebee bat"),
    ("What is the largest reptile in the world?", "saltwater crocodile"),
    ("What is the tallest mountain in the world?", "everest"),
    ("What is the largest land animal?", "elephant"),
    ("What is the largest desert in the world?", "sahara"),
    ("What is the largest ocean in the world?", "pacific"),
    ("How many letters are in the word butterfly?", "9"),
]'''

n = text.count(OLD_BLOCK)
if n != 1:
    raise SystemExit(f"FAIL: expected 1 occurrence of added-pool block, found {n}")
text = text.replace(OLD_BLOCK, NEW_BLOCK)
open(path, "w").write(text)
print("OK  pool examples replaced with same-type (different-content) examples")
