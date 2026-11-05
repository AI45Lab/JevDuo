"""golden 样例加载：samples/ 下每个 *.json 按 <category>_<name>.json 命名，
category ∈ {action, observation, receipt}。jevduo check 与 tests/protocol 共用。"""

from __future__ import annotations

import json
from collections.abc import Iterator
from pathlib import Path

SAMPLES_DIR = Path(__file__).parent / "samples"
CATEGORIES = ("action", "observation", "receipt")


def iter_samples() -> Iterator[tuple[str, dict]]:
    for file in sorted(SAMPLES_DIR.glob("*.json")):
        category = file.name.split("_", 1)[0]
        if category not in CATEGORIES:
            raise ValueError(f"样例文件名必须以 {CATEGORIES} 之一开头: {file.name}")
        yield file.stem, json.loads(file.read_text(encoding="utf-8"))
