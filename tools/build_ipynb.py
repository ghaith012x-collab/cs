#!/usr/bin/env python3
"""Build hcaptcha_superbrain.ipynb from hcaptcha_superbrain.py.

Splits the source on `# %%` / `# %% [markdown]` markers into code /
markdown notebook cells and writes a valid .ipynb that Kaggle can import.
Comment-only blocks (every line starts with `#`) become markdown cells.
"""
import json
from pathlib import Path

SRC = Path("hcaptcha_superbrain.py")
DST = Path("hcaptcha_superbrain.ipynb")


def strip_comment_prefix(lines: list[str]) -> list[str]:
    out = []
    for ln in lines:
        s = ln
        while s.startswith("# "):
            s = s[2:]
        if s.startswith("#"):
            s = s[1:]
        out.append(s)
    return out


def is_comment_only(lines: list[str]) -> bool:
    non_empty = [ln for ln in lines if ln.strip()]
    return bool(non_empty) and all(ln.lstrip().startswith("#") for ln in non_empty)


cells: list = []
cur: list | None = None
cur_kind = "code"

for line in SRC.read_text().splitlines(keepends=True):
    if line.startswith("# %% [markdown]"):
        if cur is not None:
            cells.append((cur_kind, cur))
        cur, cur_kind = [], "markdown"
    elif line.startswith("# %%"):
        if cur is not None:
            cells.append((cur_kind, cur))
        cur, cur_kind = [], "code"
    else:
        if cur is None:
            cur = []
        cur.append(line)
if cur is not None:
    cells.append((cur_kind, cur))

nb_cells = []
for kind, lines in cells:
    if kind == "markdown":
        nb_cells.append({"cell_type": "markdown", "metadata": {},
                         "source": strip_comment_prefix(lines)})
    elif is_comment_only(lines):
        nb_cells.append({"cell_type": "markdown", "metadata": {},
                         "source": strip_comment_prefix(lines)})
    else:
        nb_cells.append({"cell_type": "code", "metadata": {}, "source": lines})

nb = {
    "cells": nb_cells,
    "metadata": {
        "kernelspec": {"display_name": "Python 3", "language": "python", "name": "python3"},
        "language_info": {"name": "python", "version": "3.10"},
    },
    "nbformat": 4,
    "nbformat_minor": 5,
}
DST.write_text(json.dumps(nb, indent=1))
print(f"Built {DST} with {len(nb_cells)} cells")
