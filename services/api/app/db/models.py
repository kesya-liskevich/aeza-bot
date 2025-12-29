from sqlalchemy.orm import DeclarativeBase, Mapped, mapped_column
from sqlalchemy import String, Integer, ForeignKey, DateTime, Enum, JSON, func, Boolean
import enum

class Base(DeclarativeBase):
    pass

class TicketStatus(str, enum.Enum):
    new = "new"
    in_progress = "in_progress"
    wait_client = "wait_client"
    closed = "closed"
    escalated = "escalated"

class User(Base):
    __tablename__ = "users"
    id: Mapped[int] = mapped_column(primary_key=True)
    tg_id: Mapped[str] = mapped_column(String(64), unique=True, index=True)
    name: Mapped[str] = mapped_column(String(255))
    phone: Mapped[str] = mapped_column(String(64), nullable=True)
    company: Mapped[str] = mapped_column(String(255), nullable=True)
    lang: Mapped[str] = mapped_column(String(8), default="ru")
    created_at: Mapped[DateTime] = mapped_column(DateTime, server_default=func.now())

class Manager(Base):
    __tablename__ = "managers"
    id: Mapped[int] = mapped_column(primary_key=True)
    tg_id: Mapped[str] = mapped_column(String(64), unique=True, index=True)
    name: Mapped[str] = mapped_column(String(255))
    role: Mapped[str] = mapped_column(String(32), default="manager")
    skills: Mapped[dict] = mapped_column(JSON, default=dict)
    load_score: Mapped[int] = mapped_column(Integer, default=0)
    is_active: Mapped[bool] = mapped_column(Boolean, default=True)

class Ticket(Base):
    __tablename__ = "tickets"
    id: Mapped[int] = mapped_column(primary_key=True)
    user_id: Mapped[int] = mapped_column(ForeignKey("users.id"))
    status: Mapped[TicketStatus]
    topic: Mapped[str] = mapped_column(String(128))
    route_from: Mapped[str] = mapped_column(String(128))
    route_to: Mapped[str] = mapped_column(String(128))
    weight_kg: Mapped[int] = mapped_column(Integer, nullable=True)
    volume_cbm: Mapped[float] = mapped_column(nullable=True)
    pallets: Mapped[int] = mapped_column(Integer, nullable=True)
    incoterms: Mapped[str] = mapped_column(String(16), nullable=True)
    date_ready: Mapped[DateTime] = mapped_column(DateTime, nullable=True)
    date_delivery: Mapped[DateTime] = mapped_column(DateTime, nullable=True)
    assigned_manager_id: Mapped[int] = mapped_column(ForeignKey("managers.id"), nullable=True)
    price_hint_min: Mapped[int] = mapped_column(Integer, nullable=True)
    price_hint_max: Mapped[int] = mapped_column(Integer, nullable=True)
    eta_days: Mapped[int] = mapped_column(Integer, nullable=True)
    created_at: Mapped[DateTime] = mapped_column(DateTime, server_default=func.now())
    updated_at: Mapped[DateTime] = mapped_column(DateTime, server_default=func.now())

class Message(Base):
    __tablename__ = "messages"
    id: Mapped[int] = mapped_column(primary_key=True)
    ticket_id: Mapped[int] = mapped_column(ForeignKey("tickets.id"))
    from_role: Mapped[str] = mapped_column(String(16))
    from_id: Mapped[str] = mapped_column(String(64))
    tg_message_id: Mapped[str] = mapped_column(String(64), nullable=True)
    text: Mapped[str] = mapped_column(String(4096))
    attachments: Mapped[dict] = mapped_column(JSON, nullable=True)
    created_at: Mapped[DateTime] = mapped_column(DateTime, server_default=func.now())
