#!/usr/bin/env python3
"""ROUND 4 — brand-new fresh questions (Q7 slot replaced + full new set)."""
import fractions

GROUND_TRUTH = {}

# Q1: Integers 1..500 divisible by 7 but not by 5
GROUND_TRUTH["q1_div7_not_div5_1_500"] = len([n for n in range(1, 501) if n % 7 == 0 and n % 5 != 0])

# Q2: Last two digits of 13^13
GROUND_TRUTH["q2_13pow13_mod100"] = pow(13, 13, 100)

# Q3: Digit sum of 2^100
GROUND_TRUTH["q3_digit_sum_2pow100"] = sum(int(ch) for ch in str(2 ** 100))

# Q4: 6 painters paint 6 walls in 6h. Painters needed for 18 walls in 12h?
GROUND_TRUTH["q4_painters_18_walls_12h"] = 18 / (12 * (6 / 6 / 6))

# Q5: 150m train passes a pole in 9s. Speed in km/h?
GROUND_TRUTH["q5_train_speed_kmh"] = (150 / 9) * 3.6

# Q6: P(at least one 6) in 3 dice rolls
GROUND_TRUTH["q6_at_least_one_6_3rolls"] = fractions.Fraction(91, 216)  # 1 - (5/6)^3

# Q7: print(2 ** 3 ** 2)
GROUND_TRUTH["q7_pow_pow"] = 2 ** 3 ** 2

# Q8: print(len({1, 2, 3, 2}))
GROUND_TRUTH["q8_set_len"] = len({1, 2, 3, 2})

# Q9: print(list(range(10))[::2])
GROUND_TRUTH["q9_slice_step2"] = list(range(10))[::2]

# Q10: 20th term of 7, 12, 17, ...
GROUND_TRUTH["q10_arithmetic_20th"] = 7 + (20 - 1) * 5

# ---------------- MY LOCKED-IN ANSWERS (hand-reasoned pre-run) ----------------
MY_ANSWERS = {
    "q1_div7_not_div5_1_500": 57,
    "q2_13pow13_mod100": 53,
    "q3_digit_sum_2pow100": 115,
    "q4_painters_18_walls_12h": 9.0,
    "q5_train_speed_kmh": 60.0,
    "q6_at_least_one_6_3rolls": fractions.Fraction(91, 216),
    "q7_pow_pow": 512,
    "q8_set_len": 3,
    "q9_slice_step2": [0, 2, 4, 6, 8],
    "q10_arithmetic_20th": 102,
}

# ---------------- GRADER ----------------
total = len(GROUND_TRUTH)
correct = 0
for q in GROUND_TRUTH:
    exp, got = GROUND_TRUTH[q], MY_ANSWERS.get(q)
    if isinstance(exp, (int, float, fractions.Fraction)) and isinstance(got, (int, float, fractions.Fraction)):
        ok = abs(float(exp) - float(got)) < 1e-9
    else:
        ok = exp == got
    correct += ok
    print(f"[{'PASS' if ok else 'FAIL'}] {q}\n      expected={exp!r}\n      mine    ={got!r}")
print(f"\nSCORE: {correct}/{total}")
