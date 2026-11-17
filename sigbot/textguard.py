"""Сверка текста поста с данными движка.

Числа в пост подставляет только шаблон. LLM пишет комментарий без цифр.
Перед публикацией каждое число в итоговом тексте должно найтись среди чисел движка.
"""
from __future__ import annotations

import re

# число не внутри слова (TP1, 1000PEPE, 4h); префикс x (плечо x25) и суффикс R (+1.5R) проверяются
NUM_RE = re.compile(r"(?<![A-WYZa-wyz0-9_.])[-+]?\d[\d,]*(?:\.\d+)?(?![A-QS-Za-z0-9])")


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
