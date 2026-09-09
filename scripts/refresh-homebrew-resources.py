#!/usr/bin/env python3
"""Refresh Homebrew Python resource blocks from a published OutcomeCI release."""
from __future__ import annotations

import json
import re
import subprocess
import sys
import tempfile
import urllib.request
import venv

formula, version = sys.argv[1], sys.argv[2]
temporary = tempfile.mkdtemp()
venv.create(f"{temporary}/venv", with_pip=True)
pip = f"{temporary}/venv/bin/pip"
subprocess.check_call([pip, "install", "-q", f"outcomeci-cli=={version}"])
freeze = subprocess.check_output([pip, "list", "--format=freeze"], text=True)
dependencies = []
for line in freeze.splitlines():
    if "==" in line:
        name, dependency_version = line.split("==", 1)
        if name.casefold() not in {"outcomeci-cli", "pip", "setuptools", "wheel"}:
            dependencies.append((name, dependency_version))
blocks = []
for name, dependency_version in sorted(dependencies, key=lambda item: item[0].casefold()):
    with urllib.request.urlopen(f"https://pypi.org/pypi/{name}/{dependency_version}/json") as response:
        metadata = json.load(response)
    sdist = next(item for item in metadata["urls"] if item["packagetype"] == "sdist")
    blocks.append(f'  resource "{metadata["info"]["name"]}" do\n    url "{sdist["url"]}"\n    sha256 "{sdist["digests"]["sha256"]}"\n  end')
text = open(formula, encoding="utf-8").read()
text = re.sub(r'\n  resource "[^"]+" do\n(?:.*\n)*?  end\n', '\n', text)
text = re.sub(r'(  depends_on "python@[^"]+"\n)', r'\1\n' + "\n\n".join(blocks) + "\n", text, count=1)
open(formula, "w", encoding="utf-8").write(re.sub(r'\n{3,}', '\n\n', text))
