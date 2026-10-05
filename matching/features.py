"""Извлечение признаков из описаний по словарю ключевых слов (без ML/LLM)."""

import logging

import yaml

from config import FEATURES_PATH

log = logging.getLogger(__name__)

DEFAULT_FEATURES = {
    "кожаный салон": ["кожа", "кожаный", "leather"],
    "пневмоподвеска": ["пневмо", "пневма", "air suspension"],
    "люк/панорама": ["люк", "панорама", "панорамная крыша"],
    "полный привод": ["4wd", "awd", "полный привод", "4х4", "4x4"],
    "гибрид": ["гибрид", "hybrid", "hyb"],
    "камера заднего вида": ["камера", "задняя камера"],
    "подогрев сидений": ["подогрев"],
    "автозапуск": ["автозапуск"],
    "растаможен": ["растаможен", "растаможка"],
    "не бит, не крашен": ["не бит", "не крашен", "без покрасов"],
}


def load_features() -> dict[str, list[str]]:
    if FEATURES_PATH.exists():
        with open(FEATURES_PATH, encoding="utf-8") as f:
            return yaml.safe_load(f) or {}
    save_features(DEFAULT_FEATURES)
    return dict(DEFAULT_FEATURES)


def save_features(features: dict[str, list[str]]) -> None:
    with open(FEATURES_PATH, "w", encoding="utf-8") as f:
        yaml.safe_dump(features, f, allow_unicode=True, sort_keys=True)


def add_feature(name: str, synonyms: list[str]) -> None:
    features = load_features()
    existing = features.get(name, [])
    features[name] = sorted(set(existing) | {s.lower() for s in synonyms})
    save_features(features)


def extract(text: str | None) -> dict[str, bool]:
    """Возвращает {признак: True} для всех найденных в тексте признаков."""
    if not text:
        return {}
    lowered = text.lower()
    found = {}
    for name, synonyms in load_features().items():
        if any(syn.lower() in lowered for syn in synonyms):
            found[name] = True
    return found
