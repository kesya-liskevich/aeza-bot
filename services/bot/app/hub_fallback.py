from typing import Optional
from app.hubs import nearest_hubs


async def hub_tail_fallback_to(
    *,
    from_city: str,  # A
    to_city: str,    # B (маленький)
    tonnage: float,
    car_types: list[str],
    hubs_top_k: int = 3,
) -> Optional[dict]:
    """
    Если A→B пусто, и B маленький: находим хаб C возле B,
    берём ATI по A→C, считаем ₽/км, добавляем хвост C→B.
    Возвращает синтетику диапазоном.
    """
    A = (from_city or "").strip()
    B = (to_city or "").strip()
    if not A or not B:
        return None

    # 1) кандидаты хабов возле B
    candidates = await nearest_hubs(B, top_k=hubs_top_k)
    if not candidates:
        return None

    # 2) нормализуем тоннаж
    t = normalize_ati_tonnage(tonnage)

    # 3) считаем хвостовые км заранее (B<->C)
    dist_cb_cache: dict[str, float] = {}
    for C in candidates:
        km_cb = await distance_km(C, B)
        if km_cb and km_cb > 0:
            dist_cb_cache[C] = km_cb

    if not dist_cb_cache:
        return None

    # 4) пробуем найти ставку ATI на A→C
    for C in candidates:
        if C not in dist_cb_cache:
            continue

        # CityId обязателен
        A_id = await ati_resolve_city_id(A)
        C_id = await ati_resolve_city_id(C)
        if not A_id or not C_id:
            continue

        # ATI ставки A→C
        rates = await ati_collect_full_rates(
            from_id=A_id,
            to_id=C_id,
            tonnage=t,
            car_types=car_types or ["tent", "close"],
        )
        if not rates:
            continue

        # расстояние A→C
        dist_ac = await distance_km(A, C)
        if not dist_ac or dist_ac <= 0:
            continue

        dist_cb = dist_cb_cache[C]
        # берём минимальные/максимальные "от" по НДС-вариантам как диапазон
        rate_from_vals = [r.get("rate_from") for r in rates if isinstance(r.get("rate_from"), (int, float))]
        rate_to_vals = [r.get("rate_to") for r in rates if isinstance(r.get("rate_to"), (int, float))]

        if not rate_from_vals:
            continue

        base_from = float(min(rate_from_vals))
        base_to = float(max(rate_to_vals)) if rate_to_vals else base_from

        rub_per_km_from = base_from / dist_ac
        rub_per_km_to = base_to / dist_ac

        synth_from = base_from + rub_per_km_from * dist_cb
        synth_to = base_to + rub_per_km_to * dist_cb

        return {
            "method": "hub_tail_to",
            "from_city": A,
            "to_city": B,
            "hub": C,
            "base_route": f"{A} → {C}",
            "tonnage": t,
            "used_car_types": car_types,
            "distance_base_km": dist_ac,
            "distance_tail_km": dist_cb,
            "rub_per_km_from": rub_per_km_from,
            "rub_per_km_to": rub_per_km_to,
            "synthetic_rate_from": int(round(synth_from)),
            "synthetic_rate_to": int(round(synth_to)),
            "rates_base": rates,  # если нужно — можно не возвращать, но удобно для логов
        }

    return None
