#!/usr/bin/env python3
"""ROUND 2 — harder fresh questions. Ground truth computed, answers locked in pre-run."""
import datetime
import math
import fractions

GROUND_TRUTH = {}

# Q1: Sum of all primes in (100, 200)
def is_prime(n):
    return n > 1 and all(n % i for i in range(2, int(n ** 0.5) + 1))
GROUND_TRUTH["q1_primes_between_100_200_sum"] = sum(p for p in range(101, 200) if is_prime(p))

# Q2: 3^2023 mod 17
GROUND_TRUTH["q2_3pow2023_mod17"] = pow(3, 2023, 17)

# Q3: Ways to make $1.00 with quarters, dimes, nickels only (25q+10d+5n=100)
GROUND_TRUTH["q3_ways_make_1_dollar_qdn"] = sum(
    1 for q in range(5) for d in range(11) for n in range(21) if 25 * q + 10 * d + 5 * n == 100
)

# Q4: gcd(123456, 78901)
GROUND_TRUTH["q4_gcd_123456_78901"] = math.gcd(123456, 78901)

# Q5: Last 3 digits of 2024^2024
GROUND_TRUTH["q5_last3_digits_2024pow2024"] = pow(2024, 2024, 1000)

# Q6: P(both cards red) drawing 2 from 52 without replacement
def ncr(n, r):
    return math.comb(n, r)
red2, total2 = ncr(26, 2), ncr(52, 2)
g = math.gcd(red2, total2)
GROUND_TRUTH["q6_prob_both_red"] = (red2 // g, total2 // g)

# Q7: Binary 1101.101 in decimal
def bin_frac_to_dec(s):
    ip, fp = s.split(".")
    val = int(ip, 2)
    for i, ch in enumerate(fp, start=1):
        val += int(ch) * 2 ** (-i)
    return val
GROUND_TRUTH["q7_binary_1101_101"] = bin_frac_to_dec("1101.101")

# Q8: print(round(2.5), round(3.5))
import io, contextlib
buf = io.StringIO()
with contextlib.redirect_stdout(buf):
    print(round(2.5), round(3.5))
GROUND_TRUTH["q8_round_halves"] = buf.getvalue().strip()

# Q9: what happens on "5" + 3
try:
    result = "5" + 3
    GROUND_TRUTH["q9_str_plus_int"] = repr(result)
except TypeError:
    GROUND_TRUTH["q9_str_plus_int"] = "TypeError"

# Q10: Day of week of 2026-07-04
GROUND_TRUTH["q10_dow_2026_07_04"] = datetime.date(2026, 7, 4).strftime("%A")

# ---------------- MY LOCKED-IN ANSWERS (hand-reasoned pre-run) ----------------
MY_ANSWERS = {
    "q1_primes_between_100_200_sum": 3167,
    "q2_3pow2023_mod17": 11,
    "q3_ways_make_1_dollar_qdn": 29,
    "q4_gcd_123456_78901": 1,
    "q5_last3_digits_2024pow2024": 776,
    "q6_prob_both_red": (25, 102),
    "q7_binary_1101_101": 13.625,
    "q8_round_halves": "2 4",
    "q9_str_plus_int": "TypeError",
    "q10_dow_2026_07_04": "Saturday",
}

# ---------------- GRADER ----------------
total = len(GROUND_TRUTH)
correct = 0
for q in GROUND_TRUTH:
    exp, got = GROUND_TRUTH[q], MY_ANSWERS.get(q)
    if isinstance(exp, (int, float)) and isinstance(got, (int, float)):
        ok = abs(exp - got) < 1e-6
    else:
        ok = exp == got
    correct += ok
    print(f"[{'PASS' if ok else 'FAIL'}] {q}\n      expected={exp!r}\n      mine    ={got!r}")
print(f"\nSCORE: {correct}/{total}")
