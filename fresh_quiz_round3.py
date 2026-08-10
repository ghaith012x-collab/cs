#!/usr/bin/env python3
"""ROUND 3 — adversarial trap questions. Ground truth computed, answers locked in pre-run."""
import fractions

GROUND_TRUTH = {}

# Q1: How many times does the digit 9 appear in numbers 1..100 inclusive?
GROUND_TRUTH["q1_digit_9s_1_to_100"] = sum(str(n).count("9") for n in range(1, 101))

# Q2: 100cm rope cut so one piece is 25% longer than the other. Shorter piece?
GROUND_TRUTH["q2_rope_shorter_piece"] = fractions.Fraction(100, 1) / fractions.Fraction(9, 4)  # x + 5x/4 = 100

# Q3: Handshakes among 10 people, each pair once
GROUND_TRUTH["q3_handshakes_10"] = 10 * 9 // 2

# Q4: 30% of 50% of 400
GROUND_TRUTH["q4_30pct_of_50pct_of_400"] = 400 * 0.5 * 0.3

# Q5: $600 laptop, 25% off, then 10% tax on discounted price
GROUND_TRUTH["q5_laptop_final_price"] = 600 * 0.75 * 1.10

# Q6: 60km @30km/h then 60km @60km/h, average speed
GROUND_TRUTH["q6_avg_speed"] = fractions.Fraction(120, 1) / fractions.Fraction(3, 1)  # 2h + 1h

# Q7: Clock gains 2min/6h, set right Mon midnight. Time shown when real time is Wed 18:00
REAL_ELAPSED_H = 2 * 24 + 18  # Mon 00:00 -> Wed 18:00 = 66 h
gain_min = 2 * (REAL_ELAPSED_H // 6)  # 2 min per 6 h
shown = 18 * 60 + gain_min
GROUND_TRUTH["q7_clock_gain_time"] = shown  # minutes since midnight

# Q8: print(0.1 + 0.2 == 0.3)
GROUND_TRUTH["q8_float_eq"] = (0.1 + 0.2) == 0.3

# Q9: print(bool("False"))
GROUND_TRUTH["q9_bool_of_str_False"] = bool("False")

# Q10: smallest positive n with n % 7 == 3 and n % 11 == 5
n = 1
while not (n % 7 == 3 and n % 11 == 5):
    n += 1
GROUND_TRUTH["q10_crt_smallest_n"] = n

# ---------------- MY LOCKED-IN ANSWERS (hand-reasoned pre-run) ----------------
MY_ANSWERS = {
    "q1_digit_9s_1_to_100": 20,
    "q2_rope_shorter_piece": fractions.Fraction(400, 9),
    "q3_handshakes_10": 45,
    "q4_30pct_of_50pct_of_400": 60.0,
    "q5_laptop_final_price": 495.0,
    "q6_avg_speed": fractions.Fraction(40, 1),
    "q7_clock_gain_time": 1102,          # 18:22
    "q8_float_eq": False,
    "q9_bool_of_str_False": True,
    "q10_crt_smallest_n": 38,
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
