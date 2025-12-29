from fastapi import APIRouter, Depends, HTTPException
from pydantic import BaseModel
from sqlalchemy.orm import Session
from ..db.session import get_session
from ..db import models

router = APIRouter(prefix="/tickets", tags=["tickets"])

class TicketCreate(BaseModel):
    tg_id: str
    name: str
    topic: str
    route_from: str
    route_to: str
    weight_kg: int | None = None
    volume_cbm: float | None = None

@router.post("")
def create_ticket(payload: TicketCreate, db: Session = Depends(get_session)):
    user = db.query(models.User).filter_by(tg_id=payload.tg_id).first()
    if not user:
        user = models.User(tg_id=payload.tg_id, name=payload.name)
        db.add(user); db.flush()
    ticket = models.Ticket(
        user_id=user.id,
        status=models.TicketStatus.new,
        topic=payload.topic,
        route_from=payload.route_from,
        route_to=payload.route_to,
        weight_kg=payload.weight_kg,
        volume_cbm=payload.volume_cbm,
    )
    db.add(ticket); db.commit(); db.refresh(ticket)
    return {"ticket_id": ticket.id, "status": ticket.status}

@router.get("/{ticket_id}")
def get_ticket(ticket_id: int, db: Session = Depends(get_session)):
    t = db.query(models.Ticket).get(ticket_id)
    if not t:
        raise HTTPException(404, "not found")
    # naive user fetch (for MVP)
    from sqlalchemy import select
    u = db.query(models.User).get(t.user_id)
    return {
        "id": t.id,
        "status": t.status,
        "user": {"id": u.id, "tg_id": u.tg_id, "name": u.name} if u else None,
        "route_from": t.route_from,
        "route_to": t.route_to,
        "assigned_manager_id": t.assigned_manager_id,
        "topic": t.topic,
    }

@router.post("/{ticket_id}/close")
def close_ticket(ticket_id: int, db: Session = Depends(get_session)):
    t = db.query(models.Ticket).get(ticket_id)
    if not t:
        raise HTTPException(404, "not found")
    t.status = models.TicketStatus.closed
    db.commit()
    return {"ticket_id": t.id, "status": t.status}
