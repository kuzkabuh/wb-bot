from sqlalchemy import Column, Integer, BigInteger, String, DateTime, JSON, ForeignKey, Numeric, Date
from sqlalchemy.sql import func
from app.db.base import Base

class User(Base):
    __tablename__ = "users"
    id = Column(Integer, primary_key=True)
    tg_id = Column(BigInteger, unique=True, nullable=False, index=True)
    role = Column(String(16), default="user", nullable=False)
    created_at = Column(DateTime(timezone=True), server_default=func.now())
    last_login_at = Column(DateTime(timezone=True), nullable=True)
    settings = Column(JSON, default=dict)

class UserCredentials(Base):
    __tablename__ = "user_credentials"
    id = Column(Integer, primary_key=True)
    user_id = Column(Integer, ForeignKey("users.id", ondelete="CASCADE"), nullable=False, index=True)
    key_version = Column(Integer, default=1, nullable=False)
    wb_api_key_encrypted = Column(String, nullable=False)
    salt = Column(String, nullable=False)
    created_at = Column(DateTime(timezone=True), server_default=func.now())
    updated_at = Column(DateTime(timezone=True), server_default=func.now(), onupdate=func.now())

class Product(Base):
    __tablename__ = "products"
    id = Column(Integer, primary_key=True)
    user_id = Column(Integer, ForeignKey("users.id", ondelete="CASCADE"), nullable=False, index=True)
    nm_id = Column(BigInteger, index=True, nullable=False)
    sku = Column(String, nullable=True)
    title = Column(String, nullable=True)
    brand = Column(String, nullable=True)
    category = Column(String, nullable=True)
    created_at = Column(DateTime(timezone=True), server_default=func.now())
    updated_at = Column(DateTime(timezone=True), server_default=func.now(), onupdate=func.now())

class Sale(Base):
    __tablename__ = "sales"
    id = Column(Integer, primary_key=True)
    user_id = Column(Integer, index=True, nullable=False)
    nm_id = Column(BigInteger, index=True, nullable=False)
    date = Column(Date, index=True, nullable=False)
    qty = Column(Integer, default=0)
    revenue = Column(Numeric(14,2), default=0)
    refund_qty = Column(Integer, default=0)
    margin = Column(Numeric(14,2), nullable=True)

class Stock(Base):
    __tablename__ = "stocks"
    id = Column(Integer, primary_key=True)
    user_id = Column(Integer, index=True, nullable=False)
    nm_id = Column(BigInteger, index=True, nullable=False)
    warehouse = Column(String, index=True)
    region = Column(String, index=True)
    date = Column(Date, index=True, nullable=False)
    qty = Column(Integer, default=0)

class Order(Base):
    __tablename__ = "orders"
    id = Column(Integer, primary_key=True)
    user_id = Column(Integer, index=True, nullable=False)
    nm_id = Column(BigInteger, index=True, nullable=False)
    order_date = Column(DateTime(timezone=True), index=True, nullable=False)
    status = Column(String, index=True)
    qty = Column(Integer, default=1)
    lead_time_days = Column(Integer, nullable=True)

class SupplyPlan(Base):
    __tablename__ = "supply_plan"
    id = Column(Integer, primary_key=True)
    user_id = Column(Integer, index=True, nullable=False)
    nm_id = Column(BigInteger, index=True, nullable=False)
    region = Column(String, index=True)
    warehouse = Column(String, index=True)
    horizon_days = Column(Integer, nullable=False)
    recommended_qty = Column(Integer, default=0)
    rationale = Column(JSON, default=dict)
    generated_at = Column(DateTime(timezone=True), server_default=func.now())
