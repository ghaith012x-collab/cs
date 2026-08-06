"""One-shot repair for captcha_solver.py:
1. Remove the broken env-resolution block that landed in the wrong function (line ~727).
2. Clean up the mangled solve_hcaptcha_accessibility signature.
3. Insert proper env resolution at the start of solve_hcaptcha_accessibility.
"""
import re

with open('captcha_solver.py', 'r') as f:
    content = f.read()

ENV_GET = "os.environ.get"

# ── FIX 1: remove the broken block that landed in the wrong function ──
bad_pattern = re.compile(
    r'\n    # Ollama endpoint from env vars \(so the bot can reach a real vers\)\n'
    r'    if not ollamaa_url:\n'
    r'        ollamaa_url = ' + ENV_GET + r'\("OLLAMA_URL"\) or ' + ENV_GET +
    r'\("OLLAMA_BASE"\) or "http://localhost:11434"\n'
    r'    if not ollamaa_model:\n'
    r'        ollamaa_model = ' + ENV_GET + r'\("OLLAMA_MODEL"\) or ' + ENV_GET +
    r'\("OLLAMA_VISION_MODEL"\) or "minicpm-v"\n'
    r'    if not ollamaa_url\.endswith\("/"\):\n'
    r'        ollamaa_url \+= "/"\n'
    r'    log\(f"\[Accessibility\] Ollama = \{ollama_url\} model=\{\}"\.format\(ollama_url, ollama_model\)\)\n'
)
content, n1 = bad_pattern.subn('', content)
print(f"FIX1: removed {n1} bad block(s) from wrong function")

# ── FIX 2: clean the mangled signature ──
mangled_sig = re.compile(
    r'async def solve_hcaptcha_accessibility\(page, iframe, \n'
    r'                                        ollama_model: str = "",\n'
    r'                                            ollama_url: str = "",\n'
    r'                                            # Endpoint/model from env \(OLLAMA_URL, OLLAMA_MODEL\)\n'
    r'                                            # so a reachable vision server can be configured\.\n'
    r'                                        log: Optional\[Callable\] = None,\n'
    r'                                        max_attempts: int = 4\) -> bool:'
)
clean_sig = (
    'async def solve_hcaptcha_accessibility(page, iframe, \n'
    '                                        ollama_model: str = "",\n'
    '                                        ollama_url: str = "",\n'
    '                                        log: Optional[Callable] = None,\n'
    '                                        max_attempts: int = 4) -> bool:'
)
content, n2 = mangled_sig.subn(clean_sig, content)
print(f"FIX2: cleaned {n2} mangled signature(s)")

# ── FIX 3: insert env resolution at the start of the function body ──
anchor = '    log = log or (lambda msg, level="info": None)\n'
env_code = (
    '\n'
    '    # Ollama endpoint/model come from env vars so the bot can reach\n'
    '    # a server that actually hosts a vision model (localhost:11434\n'
    '    # only works when Ollama runs on the same machine as the bot).\n'
    '    if not ollama_url:\n'
    '        ollama_url = ' + ENV_GET + '("OLLAMA_URL") or ' + ENV_GET + \
    '("OLLAMA_BASE") or "http://localhost:11434"\n'
    '    if not ollama_model:\n'
    '        ollama_model = ' + ENV_GET + '("OLLAMA_MODEL") or ' + ENV_GET + \
    '("OLLAMA_VISION_MODEL") or "minicpm-v"\n'
    '    if not ollama_url.endswith("/"):\n'
    '        ollama_url += "/"\n'
    '    log(f"[Accessibility] Ollama endpoint: {ollama_url}  model: {ollama_model}")\n'
)
# Only insert into the accessibility function (the one that has 'frame_locator' docstring after)
idx = content.find('async def solve_hcaptcha_accessibility')
sub = content[idx:]
if anchor in sub:
    pos = sub.find(anchor)
    sub = sub[:pos + len(anchor)] + env_code + sub[pos + len(anchor):]
    content = content[:idx] + sub
    print("FIX3: inserted env resolution into solve_hcaptcha_accessibility")
else:
    print("FIX3 WARN: anchor not found in accessibility function")

with open('captcha_solver.py', 'w') as f:
    f.write(content)

print("DONE")
