from sqlalchemy import Column, Integer, String, Enum, DateTime, Text, ForeignKey
from sqlalchemy.orm import relationship
from datetime import datetime
import enum
from app.database import Base


class PCNType(str, enum.Enum):
    PCN = "PCN"
    ECN = "ECN"


class PCNFormStatus(str, enum.Enum):
    DRAFT               = "DRAFT"
    # ECN 技術類分支（製程/設計/供應商變更）
    ECN_PENDING_ENG     = "ECN_PENDING_ENG"   # ECN 待工程確認
    ECN_PENDING_QC      = "ECN_PENDING_QC"    # ECN 待品保確認
    # PCN 標準路線
    PENDING_QC          = "PENDING_QC"         # PCN 待品保 SIP
    PENDING_PRODUCTION  = "PENDING_PRODUCTION" # PCN 待產線 SOP
    # 共用
    PENDING_BU_APPROVAL = "PENDING_BU_APPROVAL"
    APPROVED            = "APPROVED"
    RETURNED            = "RETURNED"
    CLOSED              = "CLOSED"


class PCNForm(Base):
    __tablename__ = "pcn_forms"

    id                   = Column(Integer, primary_key=True, index=True)
    form_id              = Column(String(30), unique=True, nullable=False)
    type                 = Column(Enum(PCNType), default=PCNType.PCN, nullable=False)
    status               = Column(Enum(PCNFormStatus), default=PCNFormStatus.DRAFT, nullable=False)
    department           = Column(String(50), nullable=True)   # 提出部門
    product_name         = Column(String(200), nullable=False)
    product_model        = Column(String(100), nullable=True)
    change_description   = Column(Text, nullable=False)
    change_reason        = Column(Text, nullable=True)
    effective_date       = Column(String(20), nullable=True)
    change_types         = Column(Text, nullable=True)   # JSON 陣列，ECN 變更類型
    created_by           = Column(Integer, ForeignKey("users.id"), nullable=False)
    assigned_qc_id       = Column(Integer, ForeignKey("users.id"), nullable=True)
    assigned_prod_mgr_id = Column(Integer, ForeignKey("users.id"), nullable=True)
    qc_comment           = Column(Text, nullable=True)
    prod_comment         = Column(Text, nullable=True)
    created_at           = Column(DateTime, default=datetime.utcnow)
    updated_at           = Column(DateTime, default=datetime.utcnow, onupdate=datetime.utcnow)

    creator           = relationship("User", foreign_keys=[created_by])
    assigned_qc       = relationship("User", foreign_keys=[assigned_qc_id])
    assigned_prod_mgr = relationship("User", foreign_keys=[assigned_prod_mgr_id])
    documents         = relationship("PCNDocument", back_populates="form",
                                     cascade="all, delete-orphan", order_by="PCNDocument.uploaded_at")
    approvals         = relationship("PCNApproval", back_populates="form",
                                     cascade="all, delete-orphan", order_by="PCNApproval.created_at")


class PCNDocument(Base):
    __tablename__ = "pcn_documents"

    id            = Column(Integer, primary_key=True, index=True)
    form_id_fk    = Column(Integer, ForeignKey("pcn_forms.id"), nullable=False)
    filename      = Column(String(255), nullable=False)
    original_name = Column(String(255), nullable=False)
    category      = Column(String(50), nullable=True)
    uploaded_by   = Column(Integer, ForeignKey("users.id"), nullable=True)
    uploaded_at   = Column(DateTime, default=datetime.utcnow)

    form     = relationship("PCNForm", back_populates="documents")
    uploader = relationship("User", foreign_keys=[uploaded_by])


class PCNApproval(Base):
    __tablename__ = "pcn_approvals"

    id            = Column(Integer, primary_key=True, index=True)
    form_id_fk    = Column(Integer, ForeignKey("pcn_forms.id"), nullable=False)
    approver_id   = Column(Integer, ForeignKey("users.id"), nullable=False)
    action        = Column(String(20), nullable=False)
    comment       = Column(Text, nullable=True)
    reject_target = Column(String(50), nullable=True)  # 退回對象（工程師/品保/提案單位）
    from_status   = Column(String(50), nullable=True)
    to_status     = Column(String(50), nullable=True)
    created_at    = Column(DateTime, default=datetime.utcnow)

    form     = relationship("PCNForm", back_populates="approvals")
    approver = relationship("User", foreign_keys=[approver_id])
