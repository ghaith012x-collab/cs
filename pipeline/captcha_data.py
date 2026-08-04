"""Synthetic captcha training data generator.

Generates diverse captcha-style training examples for fine-tuning Qwen 2.5
to solve captchas without API calls. Covers:

- Distorted text recognition (simulated via text descriptions)
- Math equation solving
- hCaptcha accessibility fallback challenges
- Pattern/sequence completion
- Word/letter rearrangement puzzles
- Simple logic puzzles

Usage:
    python -m pipeline.captcha_data --count 2000 --output data/captcha_train.jsonl
"""

from __future__ import annotations

import argparse
import json
import random
import string
from pathlib import Path

# ---------------------------------------------------------------------------
# Template pools
# ---------------------------------------------------------------------------
INSTRUCTIONS = [
    "Solve this captcha challenge and output only the answer.",
    "What is the solution to this captcha? Return only the answer.",
    "Decode this captcha and provide the exact answer.",
    "This is a captcha — solve it and output just the result.",
    "Bypass this captcha by providing the correct answer.",
    "You are a captcha solver. Read this challenge and return only the answer.",
    "Extract the solution from this captcha-like challenge.",
    "Answer this accessibility challenge. Output only the result.",
    "This is an hCaptcha fallback. Give me the exact answer.",
    "Solve the anti-bot verification below. Output just the answer.",
]

MATH_TEMPLATES = [
    ("Solve: {a} + {b} = ?", "{c}", "math-add"),
    ("What is {a} × {b}?", "{c}", "math-mul"),
    ("Compute: {a} - {b}", "{c}", "math-sub"),
    ("Evaluate: ({a} + {b}) × {c}", "{d}", "math-complex"),
    ("What is the square root of {a}?", "{b}", "math-sqrt"),
    ("Calculate: {a} % of {b}", "{c}", "math-pct"),
    ("If x + {a} = {b}, solve for x.", "{c}", "math-algebra"),
    ("Solve the equation: {a}x = {b}", "{c}", "math-algebra2"),
    ("What is {a} + ({b} × {c})?", "{d}", "math-pemdas"),
    ("Find the value: {a}² + {b}", "{c}", "math-square"),
]

TEXT_CAPTCHA_TEMPLATES = [
    ("The distorted text reads: {text}. Transcribe it exactly.", "{text}", "text-distorted"),
    ("OCR this captcha image text: '{text}'. Just the text.", "{text}", "text-ocr"),
    ("Accessibility fallback: the captcha says '{text}'. What is it?", "{text}", "text-access"),
    ("A captcha displays scrambled text: '{text}'. Output the exact string.", "{text}", "text-scrambled"),
    ("Read this noisy CAPTCHA text: '{text}'. Return it verbatim.", "{text}", "text-noisy"),
]

LOGIC_TEMPLATES = [
    ("Complete the sequence: {seq}. What comes next?", "{answer}", "logic-sequence"),
    ("Rearrange to form a word: {scrambled}", "{word}", "logic-scramble"),
    ("Which word does NOT belong? {words}", "{odd}", "logic-odd"),
    ("Find the pattern: {pattern}. What is the rule?", "{rule}", "logic-pattern"),
    ("If {fact1} and {fact2}, what must be true?", "{conclusion}", "logic-deduction"),
    ("A {animal} has {n1} legs and travels at {n2} km/h. How far in {n3} hours?", "{dist} km", "logic-wordprob"),
]

SEQUENCES = [
    ("2, 4, 6, 8, ?", "10"),
    ("1, 1, 2, 3, 5, 8, ?", "13"),
    ("A, C, E, G, ?", "I"),
    ("2, 4, 8, 16, ?", "32"),
    ("Z, Y, X, W, ?", "V"),
    ("1, 4, 9, 16, ?", "25"),
    ("3, 6, 12, 24, ?", "48"),
    ("M, N, O, P, ?", "Q"),
    ("100, 81, 64, 49, ?", "36"),
    ("J, F, M, A, M, J, ?", "J"),
]

SCRAMBLED_WORDS = [
    ("elcyc", "cycle"),
    ("nidwo", "window"),
    ("tac", "cat"),
    ("epalpp", "apple"),
    ("rbaets", "baster"),
    ("oheptn", "photon"),
    ("sderpai", "despair"),
    ("elmmpiu", "plummet"),
    ("oezgn", "gonze"),
    ("evlos", "solve"),
]

ODD_ONE_OUT = [
    ("apple, banana, carrot, grape", "carrot"),
    ("dog, cat, horse, fish", "fish"),
    ("car, bus, train, bicycle", "bicycle"),
    ("red, blue, green, yellow", "yellow"),
    ("Python, Java, HTML, C++", "HTML"),
    ("square, circle, triangle, cube", "cube"),
    ("run, jump, swim, think", "think"),
    ("pencil, pen, marker, book", "book"),
]


# ===================================================================
# Generators
# ===================================================================
def _rand_str(length: int = 6) -> str:
    chars = string.ascii_letters + string.digits
    return "".join(random.choices(chars, k=length))


def _rand_num(lo: int = 1, hi: int = 100) -> int:
    return random.randint(lo, hi)


def generate_math_examples(count: int) -> list[dict]:
    records = []
    for _ in range(count):
        tmpl, _, tag = random.choice(MATH_TEMPLATES)
        a, b, c = _rand_num(1, 99), _rand_num(1, 99), _rand_num(1, 20)
        d = None

        if "add" in tag:
            answer = str(a + b)
        elif "mul" in tag and "pemdas" not in tag:
            answer = str(a * b)
        elif "sub" in tag:
            a, b = max(a, b), min(a, b)
            answer = str(a - b)
        elif "complex" in tag:
            answer = str((a + b) * c)
        elif "sqrt" in tag:
            a = random.choice([4, 9, 16, 25, 36, 49, 64, 81, 100])
            answer = str(int(a**0.5))
        elif "pct" in tag:
            a = random.choice([10, 20, 25, 30, 50, 75])
            answer = str(int((a / 100) * b))
        elif "algebra" in tag:
            a, b = _rand_num(1, 30), _rand_num(1, 100)
            answer = str(b - a)
        elif "algebra2" in tag:
            a, b = _rand_num(2, 10), _rand_num(10, 100)
            b = a * (b // a)  # divisible
            answer = str(b // a)
        elif "pemdas" in tag:
            answer = str(a + (b * c))
        elif "square" in tag:
            answer = str(a**2 + b)
        else:
            continue

        instruction = random.choice(INSTRUCTIONS)
        prompt = f"{instruction}\n\n{tmpl.format(a=a, b=b, c=c, d=d or answer)}"
        records.append({
            "instruction": prompt,
            "output": answer.strip(),
            "source": "captcha",
            "tag": tag,
        })
    return records


def generate_text_captcha_examples(count: int) -> list[dict]:
    records = []
    for _ in range(count):
        tmpl, _, tag = random.choice(TEXT_CAPTCHA_TEMPLATES)
        text = _rand_str(random.randint(4, 8))
        instruction = random.choice(INSTRUCTIONS)
        prompt = f"{instruction}\n\n{tmpl.format(text=text)}"
        records.append({
            "instruction": prompt,
            "output": text,
            "source": "captcha",
            "tag": tag,
        })
    return records


def generate_logic_examples(count: int) -> list[dict]:
    records = []
    for _ in range(count):
        tmpl_type = random.choice(["sequence", "scramble", "odd", "wordprob"])

        if tmpl_type == "sequence":
            seq, answer = random.choice(SEQUENCES)
            prompt = f"{random.choice(INSTRUCTIONS)}\n\nComplete the sequence: {seq}"
            records.append({
                "instruction": prompt, "output": answer,
                "source": "captcha", "tag": "logic-sequence",
            })

        elif tmpl_type == "scramble":
            scrambled, word = random.choice(SCRAMBLED_WORDS)
            prompt = f"{random.choice(INSTRUCTIONS)}\n\nUnscramble to form a word: {scrambled}"
            records.append({
                "instruction": prompt, "output": word,
                "source": "captcha", "tag": "logic-scramble",
            })

        elif tmpl_type == "odd":
            words, odd = random.choice(ODD_ONE_OUT)
            prompt = f"{random.choice(INSTRUCTIONS)}\n\nWhich one does NOT belong? {words}"
            records.append({
                "instruction": prompt, "output": odd,
                "source": "captcha", "tag": "logic-odd",
            })

        elif tmpl_type == "wordprob":
            a, b = _rand_num(1, 10), _rand_num(5, 50)
            prompt = f"{random.choice(INSTRUCTIONS)}\n\nIf a train travels at {a} km/h, how far in {b} hours?"
            records.append({
                "instruction": prompt, "output": str(a * b),
                "source": "captcha", "tag": "logic-wordprob",
            })

    return records


def generate_captcha_dataset(total: int = 3000) -> list[dict]:
    """Generate a complete captcha training dataset."""
    split = total // 3
    records = (
        generate_math_examples(split)
        + generate_text_captcha_examples(split)
        + generate_logic_examples(total - 2 * split)
    )
    random.shuffle(records)
    return records


def save_jsonl(records: list[dict], path: str) -> int:
    Path(path).parent.mkdir(parents=True, exist_ok=True)
    with open(path, "w", encoding="utf-8") as f:
        for r in records:
            f.write(json.dumps(r, ensure_ascii=False) + "\n")
    return len(records)


# ===================================================================
# CLI
# ===================================================================
def main() -> None:
    p = argparse.ArgumentParser(description="Generate synthetic captcha training data")
    p.add_argument("--count", type=int, default=3000, help="Total examples")
    p.add_argument("--output", default="data/captcha_train.jsonl")
    args = p.parse_args()

    records = generate_captcha_dataset(args.count)
    n = save_jsonl(records, args.output)
    print(f"✓ Generated {n} captcha training examples → {args.output}")


if __name__ == "__main__":
    main()
