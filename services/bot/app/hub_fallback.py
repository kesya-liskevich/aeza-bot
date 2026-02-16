from dataclasses import asdict, dataclass
from typing import Awaitable, Callable, Optional, Sequence

from app.hubs import is_hub, nearest_hubs


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
      - если город B крупный (hub), а A маленький — заменяем A на ближайший хаб C
        и считаем от базового плеча C→B + хвост A→C.
      - иначе заменяем B на C (базовое плечо A→C + хвост C→B).
    """
    logger.info("HUB ENTER from=%s to=%s", from_city, to_city)
    A = (from_city or "").strip()
    B = (to_city or "").strip()
    if not A or not B:
        return None

    A_id = await resolve_city_id(A)
    B_id = await resolve_city_id(B)

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

    replace_side = "to"
    if is_hub(B) and not is_hub(A):
        replace_side = "from"
    elif is_hub(A) and not is_hub(B):
        replace_side = "to"

    # Если одной из точек нет в ATI, обязательно заменяем именно её хабом.
    if A_id is None and B_id is not None:
        replace_side = "from"
    elif B_id is None and A_id is not None:
        replace_side = "to"

    if A_id is None and B_id is None:
        logger.info("Hub fallback: both endpoints missing in ATI, trying hubs for A=%s and B=%s", A, B)
        from_hubs = await nearest_hubs(A, top_k=hubs_top_k)
        to_hubs = await nearest_hubs(B, top_k=hubs_top_k)
        logger.info("Hub fallback: dual-side candidates A=%s -> %s | B=%s -> %s", A, from_hubs, B, to_hubs)

        if not from_hubs or not to_hubs:
            logger.warning("Hub fallback: dual-side fallback has no hub candidates")
            return None

        best_dual: Optional[HubFallbackResult] = None
        for C_from in from_hubs:
            C_from_id = await resolve_city_id(C_from)
            if not C_from_id:
                continue
            for C_to in to_hubs:
                C_to_id = await resolve_city_id(C_to)
                if not C_to_id:
                    continue

                base_distance = await distance_km(C_from, C_to)
                tail_a = await distance_km(A, C_from)
                tail_b = await distance_km(C_to, B)
                if not base_distance or base_distance <= 0:
                    continue
                if not tail_a or tail_a <= 0:
                    continue
                if not tail_b or tail_b <= 0:
                    continue

                for attempt_name, attempt_tonnage, attempt_car_types in attempts:
                    for car_type in attempt_car_types:
                        for with_nds in with_nds_options:
                            base_rate = await fetch_average_price(
                                from_city_id=C_from_id,
                                to_city_id=C_to_id,
                                car_type=car_type,
                                tonnage=attempt_tonnage,
                                with_nds=with_nds,
                            )
                            if base_rate is None:
                                continue

                            rub_per_km = base_rate / base_distance
                            total_tail = tail_a + tail_b
                            synthetic_rate = base_rate + rub_per_km * total_tail

                            candidate_result = HubFallbackResult(
                                method="hub_fallback",
                                from_city=A,
                                to_city=B,
                                hub_city=f"{C_from} / {C_to}",
                                base_route=f"{C_from} → {C_to}",
                                tonnage=attempt_tonnage,
                                car_type=car_type,
                                with_nds=with_nds,
                                base_rate_rub=base_rate,
                                base_distance_km=base_distance,
                                tail_distance_km=total_tail,
                                rub_per_km=rub_per_km,
                                synthetic_rate_rub=synthetic_rate,
                            )

                            if (
                                best_dual is None
                                or candidate_result.synthetic_rate_rub < best_dual.synthetic_rate_rub
                            ):
                                best_dual = candidate_result
                                logger.info(
                                    "Hub fallback: dual-side best hubs=%s/%s (%s tonnage=%s car=%s nds=%s synthetic=%s)",
                                    C_from,
                                    C_to,
                                    attempt_name,
                                    attempt_tonnage,
                                    car_type,
                                    with_nds,
                                    int(round(candidate_result.synthetic_rate_rub)),
                                )

        if best_dual is not None:
            logger.info(
                "Hub fallback: selected dual-side hubs=%s synthetic=%s base_route=%s",
                best_dual.hub_city,
                int(round(best_dual.synthetic_rate_rub)),
                best_dual.base_route,
            )
            return best_dual

        logger.info("Hub fallback: no ATI base rates for dual-side hubs %s→%s", A, B)
        return None

    replaced_city = A if replace_side == "from" else B
    anchor_city = B if replace_side == "from" else A

    candidates = await nearest_hubs(replaced_city, top_k=hubs_top_k)
    logger.info(
        "Hub fallback: side=%s replaced=%s anchor=%s candidates=%s",
        replace_side,
        replaced_city,
        anchor_city,
        candidates,
    )
    if not candidates:
        logger.info("Hub fallback: no hub candidates for %s", replaced_city)
        return None

    best_result: Optional[HubFallbackResult] = None

    for C in candidates:
        C_id = await resolve_city_id(C)
        if not C_id:
            logger.info("Hub fallback: CityId not found for hub=%s", C)
            continue

        logger.info(
            "Hub fallback: try hub C=%s for A=%s B=%s (attempt_packs=%s)",
            C,
            A,
            B,
            [a[0] for a in attempts],
        )

        for attempt_name, attempt_tonnage, attempt_car_types in attempts:
            logger.info(
                "Hub fallback: attempt %s side=%s anchor=%s hub=%s tonnage=%s car_types=%s",
                attempt_name,
                replace_side,
                anchor_city,
                C,
                attempt_tonnage,
                attempt_car_types,
            )

            if replace_side == "to":
                if A_id is None:
                    logger.warning("Hub fallback: CityId not found for anchor A=%s", A)
                    continue
                base_from_city_id = A_id
                base_to_city_id = C_id
                base_from_name = A
                base_to_name = C
                extra_distance = await distance_km(C, B)
                base_distance = await distance_km(A, C)
                tail_label = "C→B"
            else:
                if B_id is None:
                    logger.warning("Hub fallback: CityId not found for anchor B=%s", B)
                    continue
                base_from_city_id = C_id
                base_to_city_id = B_id
                base_from_name = C
                base_to_name = B
                extra_distance = await distance_km(A, C)
                base_distance = await distance_km(C, B)
                tail_label = "A→C"

            if not extra_distance or extra_distance <= 0:
                tail_route = f"{A}->{C}" if replace_side == "from" else f"{C}->{B}"
                logger.info(
                    "Hub fallback: distance %s unavailable (%s)",
                    tail_label,
                    tail_route,
                )
                continue

            if not base_distance or base_distance <= 0:
                logger.info(
                    "Hub fallback: base distance unavailable (%s→%s)",
                    base_from_name,
                    base_to_name,
                )
                continue

            for car_type in attempt_car_types:
                for with_nds in with_nds_options:
                    base_rate = await fetch_average_price(
                        from_city_id=base_from_city_id,
                        to_city_id=base_to_city_id,
                        car_type=car_type,
                        tonnage=attempt_tonnage,
                        with_nds=with_nds,
                    )
                    if base_rate is None:
                        logger.info(
                            "Hub fallback: no base ATI %s→%s (%s tonnage=%s car=%s nds=%s)",
                            base_from_name,
                            base_to_name,
                            attempt_name,
                            attempt_tonnage,
                            car_type,
                            with_nds,
                        )
                        continue

                    rub_per_km = base_rate / base_distance
                    synthetic_rate = base_rate + rub_per_km * extra_distance

                    candidate_result = HubFallbackResult(
                        method="hub_fallback",
                        from_city=A,
                        to_city=B,
                        hub_city=C,
                        base_route=f"{base_from_name} → {base_to_name}",
                        tonnage=attempt_tonnage,
                        car_type=car_type,
                        with_nds=with_nds,
                        base_rate_rub=base_rate,
                        base_distance_km=base_distance,
                        tail_distance_km=extra_distance,
                        rub_per_km=rub_per_km,
                        synthetic_rate_rub=synthetic_rate,
                    )

                    if (
                        best_result is None
                        or candidate_result.synthetic_rate_rub < best_result.synthetic_rate_rub
                    ):
                        best_result = candidate_result
                        logger.info(
                            "Hub fallback: best hub updated=%s for route %s→%s (%s tonnage=%s car=%s nds=%s synthetic=%s)",
                            C,
                            A,
                            B,
                            attempt_name,
                            attempt_tonnage,
                            car_type,
                            with_nds,
                            int(round(candidate_result.synthetic_rate_rub)),
                        )

    if best_result is not None:
        logger.info(
            "Hub fallback: selected cheapest hub=%s for route %s→%s synthetic=%s base_route=%s",
            best_result.hub_city,
            A,
            B,
            int(round(best_result.synthetic_rate_rub)),
            best_result.base_route,
        )
        return best_result

    logger.info("Hub fallback: no ATI base rates for %s→%s via hubs", A, B)
    return None
