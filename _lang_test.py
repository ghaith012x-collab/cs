"""Unit-test the multi-language scoring + word-puzzle detection without a
browser: import the solver module (it imports fine standalone) and re-use
the same regex logic."""
import re
import sys

sys.path.insert(0, ".")

import captcha_solver  # noqa: E402  (must import to register the module)


def score_line(line):
    """Replicate the scoring block from _read_question_text (kept in sync
    with captcha_solver.py)."""
    score = 0
    if re.search(r'\bjar\b|\bcoins?\b|\bhow many\b|\baltogether\b|\bin all\b'
                 r'|wie viele|münzen|münze|glas|wie viel|insgesamt'
                 r'|combien|pièces?|pièce|bocal|en tout|au total'
                 r'|cuántas|cuántos|monedas|frasco|en total'
                 r'|quante|monete|barattolo|in tutto', line, re.IGNORECASE):
        score += 4
    if re.search(r'\badd\b|\bput\b|\btotal\b|\bhas\b|\bstart with\b'
                 r'|hinzu|hinzufügen|legst|gibst|gibt es|enthält|füg'
                 r'|ajoute|ajouter|mets|mettre|total'
                 r'|añade|agrega|poner|tiene|total'
                 r'|aggiungi|mettere|ha|totale', line, re.IGNORECASE):
        score += 2
    if re.search(r'\banimal\b|\bcreature\b|\bbeast\b|\bliving\b.*\bthing\b|\bwhich\b.*\banimal\b'
                 r'|tier|tierisch|welches.*tier|tieren'
                 r'|animal|animaux'
                 r'|animal|animales'
                 r'|animale|animali', line, re.IGNORECASE):
        score += 5
    if re.search(r'\bcountry\b|\bcountries\b|\bnation\b|\bnations\b'
                 r'|land|länder|ländern|welches land|welches.*land'
                 r'|pays|quel pays'
                 r'|país|países|qué país|cual país'
                 r'|paese|paesi|quale paese', line, re.IGNORECASE):
        score += 5
    if re.search(r'welche farbe|welche (?:zimmer|farbe)|farbe von|welches zimmer'
                 r'|quelle couleur|quelle pièce|couleur de'
                 r'|qué color|de qué color|qué habitación|color de'
                 r'|che colore|di che colore|che stanza|colore di', line, re.IGNORECASE):
        score += 4
    if re.search(r'\b\d+\b', line):
        score += 2
    return score


CASES = [
    # (label, question, min_score)
    ("DE math", "Wie viele Münzen sind im Glas? Du hast 6 Münzen und legst 3 hinein.", 6),
    ("DE animal", "Wähle das Wort, das ein Tier darstellt.", 5),
    ("DE country", "Wähle das Land aus der Liste.", 5),
    ("DE color", "Welche Farbe hat der Himmel?", 4),
    ("FR math", "Combien de pièces y a-t-il dans le bocal ? Vous en ajoutez 3.", 6),
    ("ES animal", "Elige la palabra que representa un animal.", 5),
    ("IT math", "Quante monete ci sono nel barattolo? Ne aggiungi 3.", 6),
    ("DE word", "Entferne den ersten und letzten Buchstaben und schreibe das Wort rückwärts.", 0),
]

fails = 0
for label, q, want in CASES:
    sc = score_line(q)
    status = "OK " if sc >= want else "FAIL"
    if sc < want:
        fails += 1
    print(f"{status} {label}: score={sc} (want>={want}) :: {q[:60]}")

# Pull the REAL regexes out of captcha_solver.py so this test always
# exercises the patched code, not a stale copy.
import inspect
_src = inspect.getsource(captcha_solver)

def _grab(name):
    for line in _src.splitlines():
        line = line.strip()
        if line.startswith(name + " = r'"):
            return line[len(name) + 5 : -1]  # strip 'name = r' and trailing '
    raise SystemExit(f"{name} not found in source")

_wverb = _grab("_wverb")
_first = _grab("_first")
_last = _grab("_last")
_letter = _grab("_letter")
word_pat = re.compile(
    r'(?:' + _wverb + r'\s+(?:out\s+)?(?:the\s+|das\s+|den\s+|die\s+|le\s+|la\s+|il\s+|lo\s+)?'
    r'(' + _first + r')(?:\s*(?:and|&|und|et|y|e)\s*(?:the\s+|das\s+|den\s+|die\s+|le\s+|la\s+|il\s+|lo\s+)?(' + _last + r'))?\s+'
    + _letter +
    r'|(' + _first + r')(?:\s*(?:and|&|und|et|y|e)\s*(?:the\s+|das\s+|den\s+|die\s+|le\s+|la\s+|il\s+|lo\s+)?(' + _last + r'))?\s+'
    + _letter + r'.*' + _wverb + r')',
    re.IGNORECASE
)

WORD_CASES = [
    ("DE", "Entferne den ersten und letzten Buchstaben von dem Wort adrian und schreibe es rückwärts."),
    ("EN", "After removing the first and last letters write the remaining letters backward from the word adam"),
    ("FR", "Supprimez la première et la dernière lettre du mot adrian et écrivez-le à l'envers."),
    ("ES", "Elimina la primera y la última letra de la palabra adrian y escríbela al revés."),
]
for lang, q in WORD_CASES:
    ok = bool(word_pat.search(q))
    if not ok:
        fails += 1
    print(f"{'OK ' if ok else 'FAIL'} word-puzzle [{lang}]: {q[:60]}")

print("FAILURES:", fails)
sys.exit(1 if fails else 0)
