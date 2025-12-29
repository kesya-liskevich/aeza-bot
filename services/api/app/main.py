
# services/api/app/main.py
# -*- coding: utf-8 -*-
"""
Logistics API (FastAPI)
- /            : ping
- /v1/health   : health (redis ping)
- /v1/tickets  : create ticket (for future CRM / queue)
- /v1/rate     : accept a normalized draft and (optionally) compute/echo rate
- /v1/apply    : accept a finalized application payload for logging/archival

Design notes
- API keeps GPT-free by default (rate is computed in the bot). You can enable
  "fallback compute" by setting ENABLE_GPT_RATE=1 and providing OPENAI_API_KEY.
- Redis is optional. If available, we enqueue JSON lines for later processing.
"""

import os
import json
import logging
from typing import Optional, Any, Dict, Literal, List
from datetime import datetime

from fastapi import FastAPI, Body, HTTPException, Header, Request
from fastapi.middleware.cors import CORSMiddleware
from pydantic import BaseModel, Field

# Optional clients (safe imports)
try:
    import redis.asyncio as aioredis  # type: ignore
except Exception:  # pragma: no cover
    aioredis = None

# ---------- Config ----------
API_SECRET = os.getenv("API_SECRET", "change_me_api")
REDIS_URL = os.getenv("REDIS_URL", "redis://redis:6379/0")
ENABLE_GPT_RATE = os.getenv("ENABLE_GPT_RATE", "0") == "1"
OPENAI_API_KEY = os.getenv("OPENAI_API_KEY", "")
GPT_RATE_MODEL = os.getenv("GPT_RATE_MODEL", "gpt-4o-mini")

# ---------- Logging ----------
logging.basicConfig(level=logging.INFO, format="%(asctime)s | %(levelname)s | %(message)s")
log = logging.getLogger("api")

# ---------- Models ----------
class TicketIn(BaseModel):
    tg_id: str = Field(..., description="Telegram user id (string)")
    name: str = Field("", description="User full name if known")
    topic: Literal["question", "calc", "order", "other"] = "question"
    text: str = Field("", description="Free text (question)")
    meta: Optional[Dict[str, Any]] = None

class TicketOut(TicketIn):
    id: str
    created_at: datetime

# Flexible draft (keep shape aligned with the bot)
class QuoteDraft(BaseModel):
    # Allow any content – we only need to pass it through and archive
    data: Dict[str, Any] = Field(default_factory=dict)

class RateRequest(BaseModel):
    draft: Dict[str, Any] = Field(default_factory=dict, description="Normalized application data")
    # If the bot already computed a rate, it can pass it through;
    # otherwise API can (optionally) compute when ENABLE_GPT_RATE=1
    provided_rate_rub: Optional[int] = Field(None, ge=0)

class RateResponse(BaseModel):
    ok: bool
    rate_rub: Optional[int] = None
    source: Literal["bot", "api-gpt", "none"] = "none"

class ApplyRequest(BaseModel):
    draft: Dict[str, Any] = Field(default_factory=dict)
    rate_rub: Optional[int] = None
    client_tg_id: Optional[int] = None
    manager_topic_id: Optional[int] = None
    meta: Optional[Dict[str, Any]] = None

class ApplyResponse(BaseModel):
    ok: bool
    id: str
    created_at: datetime

# ---------- App ----------
app = FastAPI(title="Logistics API", version="1.1.0")

app.add_middleware(
    CORSMiddleware,
    allow_origins=["*"],
    allow_credentials=True,
    allow_methods=["*"],
    allow_headers=["*"],
)

redis: Optional["aioredis.Redis"] = None
_mem_tickets: List[TicketOut] = []

# ---------- Helpers ----------
def _require_auth(x_api_key: Optional[str]) -> None:
    if not API_SECRET:
        return
    if x_api_key != API_SECRET:
        raise HTTPException(status_code=401, detail="unauthorized")

async def _rpush_json(key: str, payload: Dict[str, Any]) -> None:
    if redis is None:
        return
    try:
        await redis.rpush(key, json.dumps(payload, ensure_ascii=False))
    except Exception as e:
        log.warning("Redis rpush failed: %s", e)

async def save_ticket(t: TicketIn) -> TicketOut:
    out = TicketOut(
        id=f"tkt_{int(datetime.utcnow().timestamp()*1000)}",
        created_at=datetime.utcnow(),
        **t.model_dump(),
    )
    await _rpush_json("tickets:queue", out.model_dump())
    _mem_tickets.append(out)
    if len(_mem_tickets) > 5000:
        del _mem_tickets[:-1000]
    return out

# Optional GPT fallback (disabled by default)
async def _gpt_min_rate_rub(draft: Dict[str, Any]) -> Optional[int]:
    if not (ENABLE_GPT_RATE and OPENAI_API_KEY):
        return None
    try:
        from openai import AsyncOpenAI
        import re, math, asyncio  # noqa
        cli = AsyncOpenAI(api_key=OPENAI_API_KEY)
        system = (
            "Ты логист-калькулятор. Верни ориентир минимальной ставки в RUB "
            "(только целое число, без текста, без разделителей тысяч). "
            "Смотри на расстояние, тонны/объем, тип кузова/режим, город пары. "
            "Всегда в рублях."
        )
        user = "Заявка (JSON):\n" + json.dumps(draft, ensure_ascii=False)
        resp = await cli.chat.completions.create(
            model=GPT_RATE_MODEL,
            messages=[{"role": "system", "content": system},
                      {"role": "user", "content": user}],
            temperature=0.1,
            max_tokens=10,
        )
        txt = (resp.choices[0].message.content or "").strip()
        digits = "".join(ch for ch in txt if ch.isdigit())
        if not digits:
            return None
        val = int(digits)
        return val if val > 0 else None
    except Exception as e:
        log.warning("gpt fallback failed: %s", e)
        return None

# ---------- Routes ----------
@app.get("/", tags=["health"])
async def root() -> dict:
    return {"ok": True, "service": "logistics-api", "time": datetime.utcnow().isoformat()}

@app.get("/v1/health", tags=["health"])
async def health() -> dict:
    status = {"ok": True, "redis": False}
    if redis is not None:
        try:
            status["redis"] = bool(await redis.ping())
        except Exception:
            status["redis"] = False
    return status

@app.post("/v1/tickets", response_model=TicketOut, tags=["tickets"])
async def create_ticket(
    ticket: TicketIn = Body(..., example={
        "tg_id": "125635340",
        "name": "Kesya",
        "topic": "question",
        "text": "Подскажите по ставке Москва→НН",
        "meta": {"source": "bot"},
    }),
    x_api_key: Optional[str] = Header(None, convert_underscores=False),
):
    _require_auth(x_api_key)
    try:
        created = await save_ticket(ticket)
        return created
    except Exception as e:
        log.exception("create_ticket failed: %s", e)
        raise HTTPException(status_code=500, detail="failed to save ticket")

@app.post("/v1/rate", response_model=RateResponse, tags=["rate"])
async def rate_endpoint(
    req: RateRequest,
    request: Request,
    x_api_key: Optional[str] = Header(None, convert_underscores=False),
):
    _require_auth(x_api_key)
    # Prefer the rate computed by the bot
    if req.provided_rate_rub is not None and req.provided_rate_rub > 0:
        await _rpush_json("rates:log", {
            "ts": datetime.utcnow().isoformat(),
            "ip": request.client.host if request.client else None,
            "source": "bot",
            "draft": req.draft,
            "rate_rub": req.provided_rate_rub,
        })
        return RateResponse(ok=True, rate_rub=req.provided_rate_rub, source="bot")

    # Optional fallback compute (disabled unless ENABLE_GPT_RATE=1)
    calc = await _gpt_min_rate_rub(req.draft)
    await _rpush_json("rates:log", {
        "ts": datetime.utcnow().isoformat(),
        "ip": request.client.host if request.client else None,
        "source": "api-gpt" if calc else "none",
        "draft": req.draft,
        "rate_rub": calc,
    })
    return RateResponse(ok=bool(calc), rate_rub=calc, source="api-gpt" if calc else "none")

@app.post("/v1/apply", response_model=ApplyResponse, tags=["application"])
async def apply_endpoint(
    req: ApplyRequest,
    request: Request,
    x_api_key: Optional[str] = Header(None, convert_underscores=False),
):
    _require_auth(x_api_key)
    payload = {
        "id": f"app_{int(datetime.utcnow().timestamp()*1000)}",
        "created_at": datetime.utcnow().isoformat(),
        "ip": request.client.host if request.client else None,
        "draft": req.draft,
        "rate_rub": req.rate_rub,
        "client_tg_id": req.client_tg_id,
        "manager_topic_id": req.manager_topic_id,
        "meta": req.meta or {},
    }
    await _rpush_json("applications:log", payload)
    return ApplyResponse(ok=True, id=payload["id"], created_at=datetime.fromisoformat(payload["created_at"]))

# ---------- Lifecycle ----------
@app.on_event("startup")
async def on_startup() -> None:
    global redis
    if aioredis and REDIS_URL:
        try:
            redis = aioredis.from_url(REDIS_URL, decode_responses=True)
            await redis.ping()
            log.info("Redis connected: %s", REDIS_URL)
        except Exception as e:
            redis = None
            log.warning("Redis connect failed (%s). Running without Redis.", e)
    else:
        log.info("Redis disabled or driver missing. Running without Redis.")

@app.on_event("shutdown")
async def on_shutdown() -> None:
    if redis is not None:
        try:
            await redis.aclose()
        except Exception:
            pass
