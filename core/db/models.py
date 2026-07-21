from datetime import datetime
from enum import Enum

from sqlalchemy import BigInteger, DateTime, Float, ForeignKey, Integer, String, Text, func
from sqlalchemy.orm import DeclarativeBase, Mapped, mapped_column, relationship


class Base(DeclarativeBase):
    pass


class SubTier(str, Enum):
    FREE = "free"
    BASIC = "basic"       # $9.99/mo — copy all signals
    PRO = "pro"           # $19.99/mo — AI-filtered only
    WHALE = "whale"       # $49.99/mo — priority execution


class TradeStatus(str, Enum):
    PENDING = "pending"
    EXECUTING = "executing"
    CONFIRMED = "confirmed"
    FAILED = "failed"


class User(Base):
    __tablename__ = "users"

    id: Mapped[int] = mapped_column(Integer, primary_key=True)
    telegram_id: Mapped[int] = mapped_column(BigInteger, unique=True, nullable=False)
    privy_user_id: Mapped[str | None] = mapped_column(String(128), unique=True)
    wallet_address: Mapped[str | None] = mapped_column(String(42))
    sub_tier: Mapped[SubTier] = mapped_column(String(16), default=SubTier.FREE)
    sub_expires_at: Mapped[datetime | None] = mapped_column(DateTime(timezone=True))
    copy_active: Mapped[bool] = mapped_column(default=True)
    max_position_usdc: Mapped[float] = mapped_column(Float, default=25.0)
    created_at: Mapped[datetime] = mapped_column(DateTime(timezone=True), server_default=func.now())

    copy_trades: Mapped[list["CopyTrade"]] = relationship(back_populates="user")


class DonorWallet(Base):
    __tablename__ = "donor_wallets"

    id: Mapped[int] = mapped_column(Integer, primary_key=True)
    address: Mapped[str] = mapped_column(String(42), unique=True, nullable=False)
    label: Mapped[str | None] = mapped_column(String(64))
    win_rate_30d: Mapped[float | None] = mapped_column(Float)     # 0.0 – 1.0
    roi_30d: Mapped[float | None] = mapped_column(Float)          # e.g. 0.42 = +42%
    total_volume_usdc: Mapped[float | None] = mapped_column(Float)
    active: Mapped[bool] = mapped_column(default=True)
    last_seen_at: Mapped[datetime | None] = mapped_column(DateTime(timezone=True))
    updated_at: Mapped[datetime] = mapped_column(
        DateTime(timezone=True), server_default=func.now(), onupdate=func.now()
    )

    signals: Mapped[list["TradeSignal"]] = relationship(back_populates="donor")


class TradeSignal(Base):
    __tablename__ = "trade_signals"

    id: Mapped[int] = mapped_column(Integer, primary_key=True)
    donor_id: Mapped[int] = mapped_column(ForeignKey("donor_wallets.id"), nullable=False)
    market_id: Mapped[str] = mapped_column(String(128), nullable=False)
    market_title: Mapped[str | None] = mapped_column(Text)
    side: Mapped[str] = mapped_column(String(4))          # YES / NO
    price: Mapped[float] = mapped_column(Float)
    size_usdc: Mapped[float] = mapped_column(Float)
    ai_score: Mapped[int | None] = mapped_column(Integer)           # 1–10
    ai_reason: Mapped[str | None] = mapped_column(Text)
    created_at: Mapped[datetime] = mapped_column(DateTime(timezone=True), server_default=func.now())

    donor: Mapped[DonorWallet] = relationship(back_populates="signals")
    copy_trades: Mapped[list["CopyTrade"]] = relationship(back_populates="signal")


class CopyTrade(Base):
    __tablename__ = "copy_trades"

    id: Mapped[int] = mapped_column(Integer, primary_key=True)
    user_id: Mapped[int] = mapped_column(ForeignKey("users.id"), nullable=False)
    signal_id: Mapped[int] = mapped_column(ForeignKey("trade_signals.id"), nullable=False)
    tx_hash: Mapped[str | None] = mapped_column(String(66))
    status: Mapped[TradeStatus] = mapped_column(String(16), default=TradeStatus.PENDING)
    size_usdc: Mapped[float] = mapped_column(Float)
    fill_price: Mapped[float | None] = mapped_column(Float)
    fee_usdc: Mapped[float | None] = mapped_column(Float)
    pnl_usdc: Mapped[float | None] = mapped_column(Float)
    error_msg: Mapped[str | None] = mapped_column(Text)
    created_at: Mapped[datetime] = mapped_column(DateTime(timezone=True), server_default=func.now())
    updated_at: Mapped[datetime] = mapped_column(
        DateTime(timezone=True), server_default=func.now(), onupdate=func.now()
    )

    user: Mapped[User] = relationship(back_populates="copy_trades")
    signal: Mapped[TradeSignal] = relationship(back_populates="copy_trades")
