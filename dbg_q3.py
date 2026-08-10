#!/usr/bin/env python3
import json
import urllib.request

URL = "https://ollama-production-c7dd.up.railway.app"


def ask(prompt):
    payload = {
        "model": "qwen3:1.7b", "stream": False, "keep_alive": "30m",
        "think": False,
        "options": {"temperature": 0.2, "num_predict": 16, "stop": ["\n", "."]},
        "messages": [{"role": "user", "content": prompt}],
    }
    req = urllib.request.Request(
        f"{URL}/api/chat", data=json.dumps(payload).encode(),
        headers={"Content-Type": "application/json"})
    try:
        r = json.loads(urllib.request.urlopen(req, timeout=60).read().decode())
        return r.get("message", {}).get("content", "")
    except Exception as e:
        return f"ERR {e}"


HDR = "Answer each question with exactly ONE word or number.\nNo punctuation, no explanation, no quotes.\n"

variants = {
    "elon + pilot + tallest2 (no coins)": HDR + (
        "Question: What do you call the person who flies a plane?\nAnswer: pilot\n"
        "Question: Who is the richest man in the world?\nAnswer: elon musk\n"
        "Question: What is the tallest building in the world?\nAnswer: burj khalifa\n"
        "Question: What is the tallest mountain in the world?\nAnswer: everest\n"
        "Question: Who is the richest person on earth?\nAnswer:"),
    "elon + richwoman + mostmoney + pilot": HDR + (
        "Question: Who is the richest man in the world?\nAnswer: elon musk\n"
        "Question: Who is the richest woman in the world?\nAnswer: francoise bettencourt meyers\n"
        "Question: Which person has the most money on earth?\nAnswer: elon musk\n"
        "Question: What do you call the person who flies a plane?\nAnswer: pilot\n"
        "Question: Who is the richest person on earth?\nAnswer:"),
    "current pool top4 (pilot+elon+coins)": HDR + (
        "Question: What do you call the person who flies a plane?\nAnswer: pilot\n"
        "Question: Who is the richest man in the world?\nAnswer: elon musk\n"
        "Question: You start with 6 coins in a jar. On Wednesday, you put 9 coins into the jar. How many coins are in your jar now?\nAnswer: 15\n"
        "Question: Your coin jar has 8 coins. On Monday, you add 9 coins. How many coins are in the jar?\nAnswer: 17\n"
        "Question: Who is the richest person on earth?\nAnswer:"),
}
for name, prompt in variants.items():
    raw = ask(prompt)
    print(f"{name:45} -> {raw!r}")
