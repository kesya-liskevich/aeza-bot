from .geo import distance_km
from .ati import _normalize_city_for_ati  # или откуда у тебя эта функция

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
    return _normalize_city_for_ati(city) in HUB_WHITELIST


async def nearest_hubs(city: str, top_k: int = 3) -> list[str]:
    city = _normalize_city_for_ati(city)
    scored = []

    for hub in HUB_WHITELIST:
        if hub == city:
            continue
        km = await distance_km(city, hub)
        if km:
            scored.append((km, hub))

    scored.sort(key=lambda x: x[0])
    return [h for _, h in scored[:top_k]]
