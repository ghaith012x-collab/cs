# ═══════════════════════════════════════════════════════════════
# hCaptcha Solver Trainer — Lightning.ai Edition
# 1) Save this file as  main.py  (replace the hello-world file)
# 2) In the Bash panel run:
#      pip install -q unsloth bitsandbytes Pillow && python main.py
# 3) Results land in  output/hcaptcha-solver/  — download the
#    hcaptcha-solver.zip from the file browser (left panel).
# ═══════════════════════════════════════════════════════════════

import gc
import json
import os
import random
from pathlib import Path

import torch
from PIL import Image, ImageDraw, ImageFilter

os.environ["TOKENIZERS_PARALLELISM"] = "false"

# ---------- config ----------
N_SAMPLES = 4000            # labeled tiles (half YES / half NO)
MAX_STEPS = 350             # ~15-35 min on T4
MODEL_ID  = "unsloth/Qwen2.5-VL-3B-Instruct"
OUT       = Path("output/hcaptcha-solver")
TILES     = OUT / "tiles"
for p in (OUT, TILES):
    p.mkdir(parents=True, exist_ok=True)

print(f"GPU: {torch.cuda.get_device_name(0)}")
print(f"VRAM: {torch.cuda.get_device_properties(0).total_memory / 1e9:.1f} GB")

# ---------- synthetic tile generator ----------
OBJECTS = ["bus", "car", "truck", "bicycle", "motorcycle", "boat",
           "airplane", "train", "cat", "dog", "bird", "traffic light"]


def draw_object(name, size=128):
    s = size / 128.0
    layer = Image.new("RGBA", (size, size), (0, 0, 0, 0))
    d = ImageDraw.Draw(layer)
    cx, cy = 64 * s, 64 * s

    def R(x, y, w, h, **kw):
        d.rectangle([(cx + (x - w / 2) * s, cy + (y - h / 2) * s),
                     (cx + (x + w / 2) * s, cy + (y + h / 2) * s)], **kw)

    def E(x, y, r, **kw):
        d.ellipse([cx + (x - r) * s, cy + (y - r) * s,
                   cx + (x + r) * s, cy + (y + r) * s], **kw)

    def P(points, **kw):
        d.polygon([(cx + x * s, cy + y * s) for x, y in points], **kw)

    if name == "bus":
        R(0, -8, 56, 40, fill=(220, 180, 30), outline=(20, 20, 20))
        R(-14, -14, 20, 10, fill=(150, 200, 255))
        R(8, -14, 20, 10, fill=(150, 200, 255))
        E(-16, 16, 8, fill=(25, 25, 25))
        E(16, 16, 8, fill=(25, 25, 25))
    elif name == "car":
        P([(-26, 14), (-18, -8), (14, -8), (24, 14)], fill=(200, 50, 50), outline=(20, 20, 20))
        P([(-12, -6), (-8, -14), (4, -14), (8, -6)], fill=(150, 200, 255))
        E(-10, 14, 6, fill=(25, 25, 25))
        E(10, 14, 6, fill=(25, 25, 25))
    elif name == "truck":
        R(-20, -14, 26, 36, fill=(220, 120, 30), outline=(20, 20, 20))
        R(12, -8, 18, 24, fill=(60, 120, 220), outline=(20, 20, 20))
        E(-14, 16, 6, fill=(25, 25, 25))
        E(14, 16, 6, fill=(25, 25, 25))
    elif name == "bicycle":
        E(-10, 8, 9, outline=(40, 40, 40), width=3)
        E(10, 8, 9, outline=(40, 40, 40), width=3)
        d.line([(cx - 10 * s, cy + 8 * s), (cx, cy - 8 * s)], fill=(40, 40, 40), width=3)
        d.line([(cx, cy - 8 * s), (cx + 10 * s, cy + 8 * s)], fill=(40, 40, 40), width=3)
        d.line([(cx, cy - 8 * s), (cx, cy + 4 * s)], fill=(40, 40, 40), width=3)
    elif name == "motorcycle":
        E(-8, 8, 7, fill=(25, 25, 25))
        E(8, 8, 7, fill=(25, 25, 25))
        R(0, -12, 18, 12, fill=(200, 60, 60), outline=(20, 20, 20))
        d.line([(cx - 8 * s, cy + 8 * s), (cx, cy - 12 * s)], fill=(40, 40, 40), width=3)
    elif name == "boat":
        P([(-26, 8), (26, 8), (18, 20), (-18, 20)], fill=(140, 90, 40), outline=(20, 20, 20))
        d.line([(cx, cy + 8 * s), (cx, cy - 18 * s)], fill=(40, 40, 40), width=3)
        P([(cx, cy - 18 * s), (cx + 14 * s, cy + 2 * s), (cx, cy + 2 * s)], fill=(220, 220, 220))
    elif name == "airplane":
        P([(-30, 0), (-6, -8), (6, -8), (30, 0), (6, 8), (-6, 8)], fill=(180, 180, 190), outline=(30, 30, 30))
        P([(-8, 0), (-18, 12), (-6, 6)], fill=(150, 150, 160))
        P([(8, 0), (18, -10), (6, -5)], fill=(150, 150, 160))
    elif name == "train":
        R(-24, -10, 20, 28, fill=(60, 140, 70), outline=(20, 20, 20))
        R(-6, -10, 20, 28, fill=(60, 140, 70), outline=(20, 20, 20))
        R(-20, -16, 14, 8, fill=(150, 200, 255))
        E(-16, 16, 5, fill=(25, 25, 25))
        E(2, 16, 5, fill=(25, 25, 25))
    elif name == "cat":
        E(0, -4, 14, fill=(230, 150, 60))
        P([(-10, -14), (-4, -22), (-2, -10)], fill=(230, 150, 60))
        P([(10, -14), (4, -22), (2, -10)], fill=(230, 150, 60))
        E(-5, -6, 2, fill=(20, 20, 20))
        E(5, -6, 2, fill=(20, 20, 20))
        d.line([(-12, 4), (-20, 8)], fill=(40, 40, 40), width=2)
        d.line([(12, 4), (20, 8)], fill=(40, 40, 40), width=2)
    elif name == "dog":
        E(-2, -2, 13, fill=(150, 95, 50))
        P([(-12, -12), (-8, -24), (-2, -10)], fill=(120, 75, 40))
        P([(8, -12), (4, -24), (-2, -10)], fill=(120, 75, 40))
        E(10, 2, 8, fill=(150, 95, 50))
        E(-5, -5, 2, fill=(20, 20, 20))
        E(3, -5, 2, fill=(20, 20, 20))
    elif name == "bird":
        E(0, 0, 10, fill=(70, 130, 200))
        P([(-8, 2), (-20, -8), (-4, 6)], fill=(50, 100, 170))
        P([(10, -2), (18, -8), (10, 2)], fill=(240, 180, 60))
        d.line([(8, -2), (14, 0)], fill=(240, 180, 60), width=3)
    elif name == "traffic light":
        R(0, -8, 16, 40, fill=(60, 60, 60), outline=(20, 20, 20))
        E(0, -14, 4, fill=(220, 40, 40))
        E(0, -2, 4, fill=(230, 200, 40))
        E(0, 10, 4, fill=(40, 200, 70))
        d.line([(cx, cy + 12 * s), (cx, cy + 20 * s)], fill=(40, 40, 40), width=4)
    return layer.rotate(random.uniform(-22, 22), resample=Image.BICUBIC)


def _random_bg(size=128):
    img = Image.new("RGB", (size, size), (random.randint(95, 165),) * 3)
    d = ImageDraw.Draw(img)
    for _ in range(random.randint(3, 8)):
        y0 = random.randint(0, size)
        d.rectangle([0, y0, size, y0 + random.randint(10, 40)],
                    fill=(random.randint(120, 205),) * 3)
    return img


def _add_noise(img):
    d = ImageDraw.Draw(img)
    for _ in range(random.randint(60, 120)):
        x, y = random.randint(0, img.width - 1), random.randint(0, img.height - 1)
        r = random.randint(1, 2)
        c = random.randint(0, 255)
        d.ellipse([x, y, x + r, y + r], fill=(c, c, c))
    for _ in range(random.randint(2, 5)):
        d.line([random.randint(0, img.width), random.randint(0, img.height),
                random.randint(0, img.width), random.randint(0, img.height)],
               fill=(random.randint(60, 200),) * 3, width=random.randint(1, 2))
    return img


def make_tile(name, size=128):
    bg = _random_bg(size).filter(ImageFilter.GaussianBlur(random.uniform(0.3, 0.8)))
    bg.paste(draw_object(name, size), (0, 0), draw_object(name, size))
    return _add_noise(bg)


# ---------- build dataset ----------
print(f"Generating {N_SAMPLES} labeled tiles...")
random.seed(42)
rows = []
for i in range(N_SAMPLES):
    obj = random.choice(OBJECTS)
    path = TILES / f"{i:05d}.png"
    make_tile(obj).save(path)
    if random.random() < 0.5:
        q, answer = obj, "YES"
    else:
        q = random.choice([o for o in OBJECTS if o != obj])
        answer = "NO"
    rows.append({"messages": [
        {"role": "user", "content": [
            {"type": "image", "image": str(path.resolve())},
            {"type": "text", "text": f"Is there a {q} in this image? Answer only YES or NO."}]},
        {"role": "assistant", "content": [{"type": "text", "text": answer}]}]})
    if (i + 1) % 1000 == 0:
        print(f"  {i + 1}/{N_SAMPLES}")

with open(OUT / "train.jsonl", "w") as f:
    for r in rows:
        f.write(json.dumps(r) + "\n")
print(f"✅ Dataset saved ({N_SAMPLES} samples)")
gc.collect()
torch.cuda.empty_cache()

# ---------- load model + LoRA ----------
from datasets import load_dataset
from trl import SFTConfig, SFTTrainer
from unsloth import FastVisionModel, UnslothVisionDataCollator, is_bf16_supported

print("Loading Qwen2.5-VL-3B (4-bit)...")
model, tokenizer = FastVisionModel.from_pretrained(
    MODEL_ID, max_seq_length=2048, load_in_4bit=True)

model = FastVisionModel.get_peft_model(
    model, r=16, lora_alpha=16, lora_dropout=0,
    use_gradient_checkpointing="unsloth", random_state=42)

dataset = load_dataset("json", data_files=str(OUT / "train.jsonl"), split="train")
print(f"Dataset rows: {len(dataset)}")

# ---------- train ----------
bf16 = is_bf16_supported()
training_args = SFTConfig(
    per_device_train_batch_size=2,
    gradient_accumulation_steps=4,
    warmup_steps=5,
    max_steps=MAX_STEPS,
    learning_rate=2e-4,
    fp16=not bf16,
    bf16=bf16,
    logging_steps=10,
    optim="adamw_8bit",
    weight_decay=0.01,
    lr_scheduler_type="linear",
    seed=42,
    output_dir=str(OUT / "lora"),
    save_strategy="no",          # avoids TRL checkpoint pickle crash
    report_to="none",
    remove_unused_columns=False,
    dataset_kwargs={"skip_prepare_dataset": True},
)

trainer = SFTTrainer(
    model=model,
    tokenizer=tokenizer,
    data_collator=UnslothVisionDataCollator(model, tokenizer),
    train_dataset=dataset,
    args=training_args,
)
trainer.train()
print("✅ Training done")

# ---------- save model (HF merged 16-bit = the reliable artifact) ----------
model.save_pretrained_merged(str(OUT / "merged"), tokenizer, save_method="merged_16bit")
model.save_pretrained(str(OUT / "lora_final"))
print("✅ Merged 16-bit model saved to", OUT / "merged")

# GGUF (vision GGUF is experimental — may fail, that's OK)
try:
    model.save_pretrained_gguf(str(OUT / "gguf"), tokenizer, quantization_method="q4_k_m")
    print("✅ GGUF exported to", OUT / "gguf")
except Exception as e:
    print("⚠️ GGUF export skipped (expected for vision models):", e)

# ---------- quick self-test ----------
print("\nRunning self-test on fresh tiles...")
try:
    eval_model, eval_tok = FastVisionModel.from_pretrained(str(OUT / "merged"), max_seq_length=2048)
    FastVisionModel.for_inference(eval_model)
    tmp = OUT / "eval_tmp.png"
    correct = 0
    n = 40
    for _ in range(n):
        obj = random.choice(OBJECTS)
        q = obj if random.random() < 0.5 else random.choice([o for o in OBJECTS if o != obj])
        truth = "YES" if q == obj else "NO"
        make_tile(obj).save(tmp)
        msgs = [{"role": "user", "content": [
            {"type": "image", "image": str(tmp)},
            {"type": "text", "text": f"Is there a {q} in this image? Answer only YES or NO."}]}]
        text = eval_tok.apply_chat_template(msgs, add_generation_prompt=True)
        inputs = eval_tok([text], return_tensors="pt", padding=True).to("cuda")
        out = eval_model.generate(**inputs, max_new_tokens=8, do_sample=False)
        ans = eval_tok.batch_decode(out[:, inputs["input_ids"].shape[1]:],
                                    skip_special_tokens=True)[0].strip().upper()
        if truth in ans:
            correct += 1
        del inputs, out
        torch.cuda.empty_cache()
    print(f"🎯 Self-test accuracy: {correct}/{n} ({100 * correct / n:.0f}%)")
except Exception as e:
    print("⚠️ Self-test failed:", e)

# ---------- package ----------
os.system("cd output/hcaptcha-solver && zip -r ../hcaptcha-solver.zip merged lora_final train.jsonl 2>/dev/null || true")
print("\n✅ ALL DONE")
print("   Download output/hcaptcha-solver.zip from the file browser (left panel).")
