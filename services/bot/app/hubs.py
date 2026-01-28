from typing import List
from app.geo import distance_km


def _norm_city(s: str) -> str:
    """
    Простая нормализация города:
    - lowercase
    - trim
    - ё → е
    """
    return (s or "").strip().lower().replace("ё", "е")


# Список логистических хабов (нормализованные названия)
HUB_WHITELIST = [
    "москва",
    "санкт-петербург",
    "екатеринбург",
    "новосибирск",
    "казань",
    "нижний новгород",
    "ростов-на-дону",
    "краснодар",
    "самара",
    "челябинск",
    "уфа",
    "пермь",
    "омск",
    "красноярск",
    "воронеж",
    "волгоград",
    "хабаровск",
    "владивосток",
]


def is_hub(city: str) -> bool:
    """
    Проверяет, является ли город логистическим хабом
    """
    return _norm_city(city) in HUB_WHITELIST


async def nearest_hubs(city: str, top_k: int = 3) -> List[str]:
    """
    Возвращает top_k ближайших хабов к городу
    (по расстоянию, через distance_km)
    """
    city_norm = _norm_city(city)
    scored: list[tuple[float, str]] = []

    for hub in HUB_WHITELIST:
        if hub == city_norm:
            continue

        km = await distance_km(city_norm, hub)
        if km and km > 0:
            scored.append((km, hub))

    scored.sort(key=lambda x: x[0])
    return [hub for _, hub in scored[:top_k]]
