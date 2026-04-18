import enum
from sqlalchemy import Column, Integer, String, Boolean, Enum
from app.database import Base


class Role(str, enum.Enum):
    ADMIN     = "admin"
    ENGINEER  = "engineer"
    QC        = "qc"
    PROD_MGR  = "prod_mgr"
    BU        = "bu"
    ENG_MGR   = "eng_mgr"
    PURCHASE  = "purchase"   # 採購
    ASSISTANT = "assistant"  # 業助
    SALES     = "sales"      # 業務
    HR        = "hr"         # 人事
    WAREHOUSE = "warehouse"  # 倉管


class BU(str, enum.Enum):
    ENERGY   = "儲能事業部"
    CONSUMER = "消費性事業部"


class User(Base):
    __tablename__ = "users"

    id           = Column(Integer, primary_key=True, index=True)
    username     = Column(String(50), unique=True, nullable=False)
    display_name = Column(String(100), nullable=False)
    hashed_password = Column(String(200), nullable=False)
    role         = Column(Enum(Role), nullable=False)
    bu           = Column(Enum(BU), nullable=True)
    is_active    = Column(Boolean, default=True)
    line_user_id = Column(String(100), nullable=True)
