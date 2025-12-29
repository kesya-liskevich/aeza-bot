from fastapi import APIRouter, Depends, HTTPException
from pydantic import BaseModel
from sqlalchemy.orm import Session
from ..db.session import get_session
from ..db import models

router = APIRouter(prefix="/messages", tags=["messages"])

class MessageCreate(BaseModel):
    ticket_id: int
    from_role: str
    from_id: str
    text: str

@router.post("")
def create_message(payload: MessageCreate, db: Session = Depends(get_session)):
    t = db.query(models.Ticket).get(payload.ticket_id)
    if not t:
        raise HTTPException(404, "ticket not found")
    msg = models.Message(
        ticket_id=payload.ticket_id,
        from_role=payload.from_role,
        from_id=payload.from_id,
        text=payload.text,
    )
    db.add(msg); db.commit(); db.refresh(msg)
    return {"id": msg.id}
