from __future__ import annotations


def estimate_typing_seconds(text: str, characters_per_minute: float = 330.0) -> float:
    """Оценить время набора текста руками.

    Скорость письма по умолчанию ~330 символов/мин.
    """
    if not text:
        return 0.3
    cpm = max(60.0, float(characters_per_minute))  # для защиты: не менее 1 символа/с
    seconds = (len(text) * 60.0) / cpm
    return max(0.3, seconds)
