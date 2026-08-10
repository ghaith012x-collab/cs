#!/usr/bin/env python3
import json
import urllib.request

URL = "https://ollama-production-c7dd.up.railway.app"


def raw_chat(prompt, num_predict=16):
    payload = {
        "model": "qwen3:1.7b", "stream": False, "keep_alive": "30m",
        "think": False,
        "options": {"temperature": 0.2, "num_predict": num_predict,
                    "stop": ["\n", "."]},
        "messages": [{"role": "user", "content": prompt}],
    }
    req = urllib.request.Request(
        f"{URL}/api/chat", data=json.dumps(payload).encode(),
        headers={"Content-Type": "application/json"})
    try:
        r = json.loads(urllib.request.urlopen(req, timeout=60).read().decode())
        return r.get("message", {}).get("content", ""), r.get("error")
    except Exception as e:
        return "", str(e)


def fewshot_prompt(question):
    # minimal fixed few-shot mirroring the pool entries
    return (
        "Answer each question with exactly ONE word or number.\n"
        "No punctuation, no explanation, no quotes.\n"
        "Question: What is the slowest animal?\nAnswer: sloth\n"
        "Question: Who is the richest man in the world?\nAnswer: elon musk\n"
        "Question: What is the tallest land animal?\nAnswer: giraffe\n"
        "Question: What is the capital of France?\nAnswer: paris\n"
        f"Question: {question}\nAnswer:"
    )


for q in ["Who is the richest person on earth",
          "Who is the richest person on earth?",
          "What is the slowest animal"]:
    c, e = raw_chat(fewshot_prompt(q))
    print(f"{q!r:45} -> {c!r}  err={e}")
