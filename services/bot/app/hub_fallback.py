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
    logger.info("Hub fallback: candidates for B=%s -> %s", B, candidates)
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

        requested_car_types = [c for c in car_types if c]
        expanded_car_types = requested_car_types + [
            c for c in ("tent", "close", "open", "ref") if c not in requested_car_types
        ]

        tonnage_variants = [float(tonnage)]
        if float(tonnage) != 20.0:
            tonnage_variants.append(20.0)

        attempts = [
            ("requested", tonnage_variants[0], requested_car_types),
            ("cartype_fallback", tonnage_variants[0], expanded_car_types),
        ]
        if len(tonnage_variants) > 1:
            attempts.append(("tonnage_to_20", tonnage_variants[1], expanded_car_types))

        logger.info(
            "Hub fallback: try hub C=%s for A=%s B=%s (attempt_packs=%s)",
            C,
            A,
            B,
            [a[0] for a in attempts],
        )

        for attempt_name, attempt_tonnage, attempt_car_types in attempts:
            logger.info(
                "Hub fallback: attempt %s on A→C=%s→%s tonnage=%s car_types=%s",
                attempt_name,
                A,
                C,
                attempt_tonnage,
                attempt_car_types,
            )
            for car_type in attempt_car_types:
                for with_nds in with_nds_options:
                    base_rate = await fetch_average_price(
                        from_city_id=A_id,
                        to_city_id=C_id,
                        car_type=car_type,
                        tonnage=attempt_tonnage,
                        with_nds=with_nds,
                    )
                    if base_rate is None:
                        logger.info(
                            "Hub fallback: no base ATI %s→%s (%s tonnage=%s car=%s nds=%s)",
                            A,
                            C,
                            attempt_name,
                            attempt_tonnage,
                            car_type,
                            with_nds,
                        )
                        continue

                    rub_per_km = base_rate / dist_ac
                    synthetic_rate = base_rate + rub_per_km * dist_cb

                    logger.info(
                        "Hub fallback: selected hub=%s for route %s→%s (%s tonnage=%s car=%s nds=%s base_rate=%s)",
                        C,
                        A,
                        B,
                        attempt_name,
                        attempt_tonnage,
                        car_type,
                        with_nds,
                        int(round(base_rate)),
                    )

                    return HubFallbackResult(
                        method="hub_fallback",
                        from_city=A,
                        to_city=B,
                        hub_city=C,
                        base_route=f"{A} → {C}",
                        tonnage=attempt_tonnage,
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
