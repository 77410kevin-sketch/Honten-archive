from sqlalchemy import Column, Integer, String, Boolean, Enum, DateTime, Text
from datetime import datetime
import enum
from app.database import Base


class SupplierType(str, enum.Enum):
    INTERNAL = "廠內"
    EXTERNAL = "外部"


class Supplier(Base):
    """NPI 詢價供應商主檔（廠內 / 外部）"""
    __tablename__ = "suppliers"

    id          = Column(Integer, primary_key=True, index=True)
    name        = Column(String(200), nullable=False)
    type        = Column(Enum(SupplierType), default=SupplierType.EXTERNAL, nullable=False)
    contact     = Column(String(100), nullable=True)
    email       = Column(String(200), nullable=True)
    phone       = Column(String(50), nullable=True)
    memo        = Column(Text, nullable=True)
    is_active   = Column(Boolean, default=True)
    created_at  = Column(DateTime, default=datetime.utcnow)
    updated_at  = Column(DateTime, default=datetime.utcnow, onupdate=datetime.utcnow)
