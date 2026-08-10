#!/usr/bin/env python3
"""Fresh-question AI self-test harness.
Ground truth is COMPUTED from scratch (not guessed). My answers were reasoned
by hand and locked in BEFORE running. The grader compares the two objectively.
"""
import datetime
import itertools

# ---------------- GROUND TRUTH (computed, independent) ----------------

# Q1: Sum of the first 50 prime numbers
def first_n_primes(n):
    primes, cand = [], 2
    while len(primes) < n:
        if all(cand % p for p in primes if p * p <= cand):
            primes.append(cand)
        cand += 1
    return primes

# Q2: 2027^2 + 2027 + 1
# Q3: Trailing zeros of 300!
def trailing_zeros(n):
    c = 0
    while n:
        n //= 5
        c += n
    return c

# Q4: Last 4 digits of 7^123
# Q5: 2026 (decimal) expressed in base 9
def to_base(n, b):
    digs = []
    while n:
        digs.append(n % b)
        n //= b
    return "".join(str(x) for x in reversed(digs)) or "0"

# Q6: P(two fair dice sum to a prime)
def is_prime(n):
    return n > 1 and all(n % i for i in range(2, int(n ** 0.5) + 1))

# Q7: Smaller clock angle at 3:47
# Q8: Python mutable default-arg behavior
def f(x, lst=[]):
    lst.append(x)
    return list(lst)

# Q9: Nested list comprehension [x*y for x in range(3) for y in range(x)]
# Q10: Day of week of 2025-03-01

GROUND_TRUTH = {
    "q1_sum_first_50_primes": sum(first_n_primes(50)),
    "q2_2027sq_plus_2027_plus_1": 2027 ** 2 + 2027 + 1,
    "q3_trailing_zeros_300_fact": trailing_zeros(300),
    "q4_last4_digits_7pow123": pow(7, 123, 10000),
    "q5_2026_in_base9": to_base(2026, 9),
    "q6_prob_prime_dice": (len([s for s in (a + b for a in range(1, 7) for b in range(1, 7)) if is_prime(s)]), 36),
    "q7_clock_angle_3_47": None,  # computed below
    "q8_mutable_default_arg": [f(1), f(2), f(3)],
    "q9_nested_comprehension": [x * y for x in range(3) for y in range(x)],
    "q10_dow_2025_03_01": datetime.date(2025, 3, 1).strftime("%A"),
}
h, m = 3, 47
ha = (h % 12) * 30 + m * 0.5
ma = m * 6
diff = abs(ha - ma)
GROUND_TRUTH["q7_clock_angle_3_47"] = round(min(diff, 360 - diff), 4)

# ---------------- MY LOCKED-IN ANSWERS (reasoned by hand, pre-run) ----------------

MY_ANSWERS = {
    "q1_sum_first_50_primes": 5117,
    "q2_2027sq_plus_2027_plus_1": 4110757,
    "q3_trailing_zeros_300_fact": 74,
    "q4_last4_digits_7pow123": 6343,
    "q5_2026_in_base9": "2701",
    "q6_prob_prime_dice": (15, 36),          # 15/36 = 5/12
    "q7_clock_angle_3_47": 168.5,
    "q8_mutable_default_arg": [[1], [1, 2], [1, 2, 3]],
    "q9_nested_comprehension": [0, 0, 2],
    "q10_dow_2025_03_01": "Saturday",
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
