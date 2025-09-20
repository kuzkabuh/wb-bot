# app/db/models.py
from __future__ import annotations

from datetime import datetime

from sqlalchemy import (
    Column,
    Integer,
    BigInteger,
    String,
    DateTime,
    Date,
    Numeric,
    JSON,
    ForeignKey,
    Text,
    func,
    UniqueConstraint,
    Index,
)
from sqlalchemy.orm import relationship

from app.db.base import Base


# =========================
# Пользователи и креды
# =========================
class User(Base):
    __tablename__ = "users"

    id = Column(Integer, primary_key=True)
    tg_id = Column(BigInteger, unique=True, nullable=False, index=True)
    role = Column(String(16), default="user", nullable=False)
    created_at = Column(DateTime(timezone=True), server_default=func.now(), nullable=False)
    last_login_at = Column(DateTime(timezone=True), nullable=True)
    settings = Column(JSON, default=dict, nullable=True)  # jsonb в Postgres

    # relations
    credentials = relationship(
        "UserCredentials",
        back_populates="user",
        uselist=False,
        cascade="all, delete-orphan",
        passive_deletes=True,
    )
    products = relationship(
        "Product",
        back_populates="user",
        cascade="all, delete-orphan",
        passive_deletes=True,
    )


class UserCredentials(Base):
    __tablename__ = "user_credentials"

    id = Column(Integer, primary_key=True)
    user_id = Column(Integer, ForeignKey("users.id", ondelete="CASCADE"), nullable=False, index=True)
    key_version = Column(Integer, default=1, nullable=False)
    wb_api_key_encrypted = Column(String, nullable=False)
    salt = Column(String, nullable=False)
    created_at = Column(DateTime(timezone=True), server_default=func.now(), nullable=False)
    updated_at = Column(
        DateTime(timezone=True), server_default=func.now(), onupdate=func.now(), nullable=False
    )

    # relations
    user = relationship("User", back_populates="credentials")


# =========================
# Номенклатура и данные
# =========================
class Product(Base):
    __tablename__ = "products"
    __table_args__ = (
        UniqueConstraint("user_id", "nm_id", name="uq_products_user_nm"),
        Index("ix_products_user_nm", "user_id", "nm_id"),
    )

    id = Column(Integer, primary_key=True)
    user_id = Column(Integer, ForeignKey("users.id", ondelete="CASCADE"), nullable=False, index=True)
    nm_id = Column(BigInteger, nullable=False)
    sku = Column(String, nullable=True)
    title = Column(String, nullable=True)
    brand = Column(String, nullable=True)
    category = Column(String, nullable=True)
    created_at = Column(DateTime(timezone=True), server_default=func.now(), nullable=False)
    updated_at = Column(
        DateTime(timezone=True), server_default=func.now(), onupdate=func.now(), nullable=False
    )

    # relations
    user = relationship("User", back_populates="products")


class Sale(Base):
    __tablename__ = "sales"
    __table_args__ = (
        UniqueConstraint("user_id", "nm_id", "date", name="uq_sales_user_nm_date"),
        Index("ix_sales_user_date", "user_id", "date"),
        Index("ix_sales_user_nm", "user_id", "nm_id"),
    )

    id = Column(Integer, primary_key=True)
    user_id = Column(Integer, index=True, nullable=False)
    nm_id = Column(BigInteger, index=True, nullable=False)
    date = Column(Date, index=True, nullable=False)
    qty = Column(Integer, default=0, nullable=False)
    revenue = Column(Numeric(14, 2), default=0, nullable=False)
    refund_qty = Column(Integer, default=0, nullable=False)
    margin = Column(Numeric(14, 2), nullable=True)


class Stock(Base):
    __tablename__ = "stocks"
    __table_args__ = (
        UniqueConstraint("user_id", "nm_id", "warehouse", "date", name="uq_stocks_user_nm_wh_date"),
        Index("ix_stocks_user_date", "user_id", "date"),
        Index("ix_stocks_user_nm", "user_id", "nm_id"),
        Index("ix_stocks_wh_region", "warehouse", "region"),
    )

    id = Column(Integer, primary_key=True)
    user_id = Column(Integer, index=True, nullable=False)
    nm_id = Column(BigInteger, index=True, nullable=False)
    warehouse = Column(String, index=True, nullable=True)
    region = Column(String, index=True, nullable=True)
    date = Column(Date, index=True, nullable=False)
    qty = Column(Integer, default=0, nullable=False)


class Order(Base):
    __tablename__ = "orders"
    __table_args__ = (
        Index("ix_orders_user_dt", "user_id", "order_date"),
        Index("ix_orders_user_nm", "user_id", "nm_id"),
    )

    id = Column(Integer, primary_key=True)
    user_id = Column(Integer, index=True, nullable=False)
    nm_id = Column(BigInteger, index=True, nullable=False)
    order_date = Column(DateTime(timezone=True), index=True, nullable=False)
    status = Column(String, index=True, nullable=True)
    qty = Column(Integer, default=1, nullable=False)
    lead_time_days = Column(Integer, nullable=True)


class SupplyPlan(Base):
    __tablename__ = "supply_plan"
    __table_args__ = (
        UniqueConstraint(
            "user_id", "nm_id", "region", "warehouse", "horizon_days", name="uq_supply_plan_key"
        ),
        Index("ix_supply_plan_user_nm", "user_id", "nm_id"),
    )

    id = Column(Integer, primary_key=True)
    user_id = Column(Integer, index=True, nullable=False)
    nm_id = Column(BigInteger, index=True, nullable=False)
    region = Column(String, index=True, nullable=True)
    warehouse = Column(String, index=True, nullable=True)
    horizon_days = Column(Integer, nullable=False)
    recommended_qty = Column(Integer, default=0, nullable=False)
    rationale = Column(JSON, default=dict, nullable=True)
    generated_at = Column(DateTime(timezone=True), server_default=func.now(), nullable=False)


# =========================
# История релизов (для auto_release)
# =========================
class ReleaseHistory(Base):
    __tablename__ = "release_history"

    id = Column(Integer, primary_key=True, index=True)
    version = Column(String(50), nullable=False)
    message = Column(Text, nullable=False)
    created_at = Column(DateTime(timezone=True), server_default=func.now(), nullable=False)
