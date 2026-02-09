from dataclasses import asdict, dataclass
from typing import Awaitable, Callable, Optional, Sequence

from app.hubs import nearest_hubs


@dataclass(frozen=True)
class HubFallbackResult:
    method: str
    from_city: str
    to_city: str
    hub_city: str
    base_route: str
    tonnage: float
    car_type: str
    with_nds: bool
    base_rate_rub: float
    base_distance_km: float
    tail_distance_km: float
    rub_per_km: float
    synthetic_rate_rub: float
    synthetic: bool = True

    def to_dict(self) -> dict:
        return asdict(self)


async def hub_fallback_pipeline(
    *,
    from_city: str,
    to_city: str,
    tonnage: float,
    car_types: Sequence[str],
    with_nds_options: Sequence[bool] = (False, True),
    hubs_top_k: int = 3,
    resolve_city_id: Callable[[str], Awaitable[Optional[int]]],
    fetch_average_price: Callable[..., Awaitable[Optional[float]]],
    distance_km: Callable[[str, str], Awaitable[Optional[float]]],
    logger,
) -> Optional[HubFallbackResult]:
    """
    Hub fallback A→C→B:
      1) выбираем хаб C рядом с B
      2) проверяем CityId для A и C
      3) получаем реальную ставку ATI A→C
      4) считаем synthetic A→B через ₽/км * distance(C→B)
    """
    logger.info("HUB ENTER from=%s to=%s", from_city, to_city)
    A = (from_city or "").strip()
    B = (to_city or "").strip()
    if not A or not B:
        return None

    candidates = await nearest_hubs(B, top_k=hubs_top_k)
    if not candidates:
        logger.info("Hub fallback: no hub candidates for %s", B)
        return None

    A_id = await resolve_city_id(A)
    if not A_id:
        logger.warning("Hub fallback: CityId not found for A=%s", A)
        return None

    for C in candidates:
        C_id = await resolve_city_id(C)
        if not C_id:
            logger.info("Hub fallback: CityId not found for hub=%s", C)
            continue

        dist_cb = await distance_km(C, B)
        if not dist_cb or dist_cb <= 0:
            logger.info("Hub fallback: distance C→B unavailable (%s→%s)", C, B)
            continue

        dist_ac = await distance_km(A, C)
        if not dist_ac or dist_ac <= 0:
            logger.info("Hub fallback: distance A→C unavailable (%s→%s)", A, C)
            continue

        for car_type in car_types:
            for with_nds in with_nds_options:
                base_rate = await fetch_average_price(
                    from_city_id=A_id,
                    to_city_id=C_id,
                    car_type=car_type,
                    tonnage=tonnage,
                    with_nds=with_nds,
                )
                if base_rate is None:
                    continue

                rub_per_km = base_rate / dist_ac
                synthetic_rate = base_rate + rub_per_km * dist_cb

                return HubFallbackResult(
                    method="hub_fallback",
                    from_city=A,
                    to_city=B,
                    hub_city=C,
                    base_route=f"{A} → {C}",
                    tonnage=tonnage,
                    car_type=car_type,
                    with_nds=with_nds,
                    base_rate_rub=base_rate,
                    base_distance_km=dist_ac,
                    tail_distance_km=dist_cb,
                    rub_per_km=rub_per_km,
                    synthetic_rate_rub=synthetic_rate,
                )

    logger.info("Hub fallback: no ATI base rates for %s→%s via hubs", A, B)
    return None
