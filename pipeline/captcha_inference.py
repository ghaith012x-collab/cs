"""Local captcha solver using your trained Ollama model.

Handles text-based captcha challenges:
- Math equations ("What is 23 + 47?")
- Accessibility fallback text
- Pattern/sequence recognition
- Simple word problems
- Scrambled words

For image-based captchas (hCaptcha grid, FunCAPTCHA tiles), use the
existing captcha_solver.py which has pixel-analysis fallbacks.

Usage:
    from pipeline.captcha_inference import LocalCaptchaSolver
    solver = LocalCaptchaSolver(model="captcha-solver-v1")
    answer = solver.solve("What is 15 + 27?")
"""

from __future__ import annotations

import json
import subprocess
import time
from typing import Optional


class LocalCaptchaSolver:
    """Solve text-based captchas using a locally trained Ollama model."""

    def __init__(
        self,
        model: str = "captcha-solver-v1",
        temperature: float = 0.1,
        timeout: float = 10.0,
    ):
        self.model = model
        self.temperature = temperature
        self.timeout = timeout
        self.stats = {"solved": 0, "failed": 0, "total_ms": 0.0}
        self._verified = False

    def _verify_model(self) -> bool:
        """Check that Ollama is running and the model exists."""
        if self._verified:
            return True
        try:
            result = subprocess.run(
                ["ollama", "list"],
                capture_output=True,
                text=True,
                timeout=5,
            )
            if self.model in result.stdout:
                self._verified = True
                return True
            print(f"[CaptchaSolver] Model '{self.model}' not found in ollama list")
            print(f"  Run: ollama create {self.model} -f Modelfile")
            return False
        except FileNotFoundError:
            print("[CaptchaSolver] Ollama is not installed")
            print("  Install: curl -fsSL https://ollama.com/install.sh | sh")
            return False
        except Exception as e:
            print(f"[CaptchaSolver] Ollama check failed: {e}")
            return False

    def solve(self, challenge: str) -> Optional[str]:
        """Solve a text captcha challenge. Returns the answer or None."""
        if not self._verify_model():
            return None

        started = time.time()
        prompt = (
            "You are a captcha solver. Solve this challenge and output ONLY the "
            "exact answer — no explanation, no extra text, just the answer.\n\n"
            f"Challenge: {challenge}"
        )

        try:
            result = subprocess.run(
                ["ollama", "run", self.model, prompt],
                capture_output=True,
                text=True,
                timeout=self.timeout,
            )
            elapsed = (time.time() - started) * 1000
            answer = result.stdout.strip()

            # Clean common noise
            for prefix in ("answer:", "Answer:", "ANSWER:", "the answer is "):
                if answer.lower().startswith(prefix):
                    answer = answer[len(prefix):].strip()

            if answer:
                self.stats["solved"] += 1
                self.stats["total_ms"] += elapsed
                return answer
            else:
                self.stats["failed"] += 1
                return None

        except subprocess.TimeoutExpired:
            self.stats["failed"] += 1
            return None
        except Exception:
            self.stats["failed"] += 1
            return None

    def solve_batch(self, challenges: list[str]) -> list[Optional[str]]:
        """Solve multiple captcha challenges sequentially."""
        return [self.solve(c) for c in challenges]

    @property
    def avg_time_ms(self) -> float:
        if self.stats["solved"] == 0:
            return 0
        return self.stats["total_ms"] / self.stats["solved"]

    @property
    def success_rate(self) -> float:
        total = self.stats["solved"] + self.stats["failed"]
        if total == 0:
            return 0
        return self.stats["solved"] / total


# ---------------------------------------------------------------------------
# Quick test
# ---------------------------------------------------------------------------
def benchmark() -> None:
    """Run a quick benchmark against known captcha challenges."""
    solver = LocalCaptchaSolver()
    tests = [
        ("What is 23 + 47?", "70"),
        ("Complete: 2, 4, 8, 16, ?", "32"),
        ("Unscramble: tac", "cat"),
        ("Calculate: 15 × 4", "60"),
        ("Which doesn't belong: dog, cat, fish, horse?", "fish"),
        ("Solve: 100 - 37", "63"),
        ("What is the square root of 64?", "8"),
    ]

    print("=" * 50)
    print("  Captcha Solver Benchmark")
    print("=" * 50)
    correct = 0
    for challenge, expected in tests:
        answer = solver.solve(challenge)
        ok = "✓" if answer and answer.strip() == expected else "✗"
        if ok == "✓":
            correct += 1
        print(f"  {ok}  {challenge}")
        print(f"      expected={expected}  got={answer}")
        print()
    print(f"  Score: {correct}/{len(tests)} ({100*correct/len(tests):.0f}%)")
    print(f"  Avg time: {solver.avg_time_ms:.0f}ms")


if __name__ == "__main__":
    benchmark()
