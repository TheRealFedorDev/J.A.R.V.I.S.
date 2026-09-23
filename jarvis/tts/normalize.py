"""Текстовая нормализация перед озвучкой.

Разворачивает сокращения и единицы измерения в полные формы, чтобы
синтезатор читал их естественно:
  «5 м/с, 25°C, 30%» → «5 метров в секунду, 25 градусов Цельсия, 30 процентов».
"""

from __future__ import annotations

import re

# Порог кириллицы: >30% слов с кириллицей → русский текст.
CYRILLIC_WORD_RE = re.compile(r"[а-яё]+", re.IGNORECASE)

NORMALIZE_RULES: list[tuple[re.Pattern, str]] = [
    # Скорость / ускорение
    (re.compile(r"\bм/с²\b", re.IGNORECASE), "метров в секунду квадратных"),
    (re.compile(r"\bм/с\b", re.IGNORECASE), "метров в секунду"),
    (re.compile(r"\bкм/ч²\b", re.IGNORECASE), "километров в час квадратных"),
    (re.compile(r"\bкм/ч\b", re.IGNORECASE), "километров в час"),
    (re.compile(r"\bмиль/ч\b", re.IGNORECASE), "миль в час"),
    (re.compile(r"\bфут/с\b", re.IGNORECASE), "футов в секунду"),
    # Температура
    (re.compile(r"(\d+)\s*°C", re.IGNORECASE), r"\1 градусов Цельсия"),
    (re.compile(r"(\d+)\s*°F", re.IGNORECASE), r"\1 градусов Фаренгейта"),
    (re.compile(r"(\d+)\s*°K", re.IGNORECASE), r"\1 градусов Кельвина"),
    (re.compile(r"(\d+)\s*°"), r"\1 градусов"),
    # Проценты
    (re.compile(r"(\d+(?:[.,]\d+)?)\s*%"), r"\1 процентов"),
    # Площадь / объём
    (re.compile(r"\bм²\b"), "квадратных метров"),
    (re.compile(r"\bм³\b"), "кубических метров"),
    (re.compile(r"\bкм²\b"), "квадратных километров"),
    (re.compile(r"\bкм³\b"), "кубических километров"),
    # Частота
    (re.compile(r"(\d+(?:[.,]\d+)?)\s*Гц\b"), r"\1 герц"),
    (re.compile(r"(\d+(?:[.,]\d+)?)\s*кГц\b"), r"\1 килогерц"),
    (re.compile(r"(\d+(?:[.,]\d+)?)\s*МГц\b"), r"\1 мегагерц"),
    (re.compile(r"(\d+(?:[.,]\d+)?)\s*ГГц\b"), r"\1 гигагерц"),
    (re.compile(r"(\d+(?:[.,]\d+)?)\s*GHz\b", re.IGNORECASE), r"\1 гигагерц"),
    (re.compile(r"(\d+(?:[.,]\d+)?)\s*MHz\b", re.IGNORECASE), r"\1 мегагерц"),
    (re.compile(r"(\d+(?:[.,]\d+)?)\s*kHz\b", re.IGNORECASE), r"\1 килогерц"),
    (re.compile(r"(\d+(?:[.,]\d+)?)\s*Hz\b"), r"\1 герц"),
    # Масса / вес
    (re.compile(r"(\d+(?:[.,]\d+)?)\s*kg\b"), r"\1 килограммов"),
    (re.compile(r"(\d+(?:[.,]\d+)?)\s*г\b"), r"\1 граммов"),
    (re.compile(r"(\d+(?:[.,]\d+)?)\s*g\b"), r"\1 граммов"),
    # Давление
    (re.compile(r"(\d+(?:[.,]\d+)?)\s*кПа\b"), r"\1 килопаскалей"),
    (re.compile(r"(\d+(?:[.,]\d+)?)\s*Па\b"), r"\1 паскалей"),
    # Мощность
    (re.compile(r"(\d+(?:[.,]\d+)?)\s*кВт\b"), r"\1 киловатт"),
    (re.compile(r"(\d+(?:[.,]\d+)?)\s*Вт\b"), r"\1 ватт"),
    # Разделители в больших числах: 1 000 → 1000
    (re.compile(r"\b(\d{1,3}(?:\s\d{3})+)\b"), lambda m: m.group(1).replace(" ", "")),
]


def is_russian(text: str) -> bool:
    """True, если >30% слов написаны кириллицей."""
    words = text.split()
    if not words:
        return False
    cyrillic_words = sum(1 for w in words if CYRILLIC_WORD_RE.search(w))
    return (cyrillic_words / len(words)) > 0.3


def normalize_text(text: str) -> str:
    """Разворачивает сокращения и единицы измерения в полные формы."""
    for pattern, replacement in NORMALIZE_RULES:
        text = pattern.sub(replacement, text)
    return text
