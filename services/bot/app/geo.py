# ===== GEO / DISTANCE (Redis cache) =====

GEO_KEY = "geo:city:{name}"                  # -> "lat,lon"
DIST_KEY = "dist:km:{a}::{b}"                # -> float str
_GEO_TTL = 180 * 24 * 60 * 60                # 180 дней
_DIST_TTL = 365 * 24 * 60 * 60               # 365 дней


def _norm_geo_city(name: str) -> str:
    # используем ту же нормализацию, что и для ATI (важно!)
    return _normalize_city_for_ati(name)


async def geo_resolve_city_latlon(city_name: str) -> Optional[tuple[float, float]]:
    """
    Возвращает (lat, lon) для города через Nominatim.
    Сильно кэшируем в Redis.
    """
    norm = _norm_geo_city(city_name)
    if not norm:
        return None

    key = GEO_KEY.format(name=norm)
    cached = await redis.get(key)
    if cached:
        try:
            lat_s, lon_s = cached.split(",", 1)
            return float(lat_s), float(lon_s)
        except Exception:
            pass

    # Nominatim query
    params = {
        "q": norm,
        "format": "json",
        "limit": 1,
        "addressdetails": 0,
    }

    headers = {"User-Agent": NOMINATIM_UA, "Accept": "application/json"}

    try:
        async with aiohttp.ClientSession() as s:
            async with s.get(
                f"{NOMINATIM_BASE_URL}/search",
                params=params,
                headers=headers,
                timeout=15,
            ) as r:
                if r.status != 200:
                    log.warning("Nominatim status=%s city=%r", r.status, city_name)
                    return None
                data = await r.json()
    except Exception as e:
        log.warning("Nominatim error city=%r: %s", city_name, e)
        return None

    if not isinstance(data, list) or not data:
        log.warning("Nominatim empty city=%r norm=%r", city_name, norm)
        return None

    try:
        lat = float(data[0]["lat"])
        lon = float(data[0]["lon"])
    except Exception:
        return None

    await redis.set(key, f"{lat},{lon}", ex=_GEO_TTL)
    return lat, lon


async def osrm_distance_km(lat1: float, lon1: float, lat2: float, lon2: float) -> Optional[float]:
    """
    Дорожное расстояние OSRM (driving), возвращает км.
    """
    # OSRM требует lon,lat
    coords = f"{lon1},{lat1};{lon2},{lat2}"
    url = f"{OSRM_BASE_URL}/route/v1/driving/{coords}"
    params = {"overview": "false", "alternatives": "false", "steps": "false"}

    try:
        async with aiohttp.ClientSession() as s:
            async with s.get(url, params=params, timeout=15) as r:
                if r.status != 200:
                    txt = await r.text()
                    log.warning("OSRM status=%s body=%s", r.status, txt[:200])
                    return None
                data = await r.json()
    except Exception as e:
        log.warning("OSRM error: %s", e)
        return None

    try:
        routes = data.get("routes") or []
        if not routes:
            return None
        dist_m = routes[0].get("distance")
        if not isinstance(dist_m, (int, float)) or dist_m <= 0:
            return None
        return float(dist_m) / 1000.0
    except Exception:
        return None


async def distance_km(city_a: str, city_b: str) -> Optional[float]:
    """
    Дорожное расстояние между ГОРОДАМИ (центры) через:
      - geo_resolve_city_latlon
      - OSRM
    Кэшируем.
    """
    a = _norm_geo_city(city_a)
    b = _norm_geo_city(city_b)
    if not a or not b:
        return None

    # симметричный ключ, чтобы не хранить A->B и B->A отдельно
    a1, b1 = (a, b) if a <= b else (b, a)
    key = DIST_KEY.format(a=a1, b=b1)

    cached = await redis.get(key)
    if cached:
        try:
            return float(cached)
        except Exception:
            pass

    p1 = await geo_resolve_city_latlon(a)
    p2 = await geo_resolve_city_latlon(b)
    if not p1 or not p2:
        return None

    km = await osrm_distance_km(p1[0], p1[1], p2[0], p2[1])
    if km is None:
        return None

    await redis.set(key, str(km), ex=_DIST_TTL)
    return km
