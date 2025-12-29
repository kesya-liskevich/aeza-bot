from fastapi import APIRouter, Depends, HTTPException
from pydantic import BaseModel
from sqlalchemy.orm import Session
from ..db.session import get_session
from ..db import models

router = APIRouter(prefix="/assign", tags=["assign"])

class AssignRequest(BaseModel):
    ticket_id: int
    manager_tg_id: str

@router.post("")
def assign_ticket(req: AssignRequest, db: Session = Depends(get_session)):
    t = db.query(models.Ticket).get(req.ticket_id)
    if not t:
        raise HTTPException(404, "ticket not found")
    m = db.query(models.Manager).filter_by(tg_id=req.manager_tg_id, is_active=True).first()
    if not m:
        m = models.Manager(tg_id=req.manager_tg_id, name=f"mgr_{req.manager_tg_id}")
        db.add(m); db.flush()
    t.assigned_manager_id = m.id
    t.status = models.TicketStatus.in_progress
    db.commit()
    return {"ticket_id": t.id, "assigned_manager_id": m.id}
