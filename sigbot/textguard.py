"""Сверка текста поста с данными движка.

Числа в пост подставляет только шаблон. LLM пишет комментарий без цифр.
Перед публикацией каждое число в итоговом тексте должно найтись среди чисел движка.
"""
from __future__ import annotations

import re

NUM_RE = re.compile(r"(?<![\w.])[-+]?\d[\d,]*(?:\.\d+)?")


class NumberMismatch(Exception):
    pass


def numbers_in(text: str) -> list[float]:
    out = []
    for m in NUM_RE.findall(text):
        try:
            out.append(float(m.replace(",", "")))
        except ValueError:
            pass
    return out


def llm_text_has_no_digits(text: str) -> bool:
    return not re.search(r"\d", text)


def verify(text: str, engine_numbers: list[float], rel_tol: float = 1e-6) -> None:
    """Бросает NumberMismatch, если в тексте есть число, которого нет у движка."""
    allowed = [float(x) for x in engine_numbers]
    bad = []
    for x in numbers_in(text):
        if not any(abs(x - a) <= max(rel_tol * abs(a), 1e-9) for a in allowed):
            bad.append(x)
    if bad:
        raise NumberMismatch(f"numbers not from engine: {bad}")
