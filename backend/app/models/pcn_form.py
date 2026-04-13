"""
PCN/ECN 模組 — 開發轉量產與產品變更
流程：工程師建立 → 品保填 SIP → 產線主管填 SOP → BU Head 審核 → 核准通知
"""
from sqlalchemy import Column, Integer, String, Boolean, Enum, DateTime, Text, ForeignKey
from sqlalchemy.orm import relationship
from datetime import datetime
import enum

from app.database import Base


# ────────────────────────────────────────────
# Enums
# ────────────────────────────────────────────

class PCNType(str, enum.Enum):
    PCN = "PCN"   # 開發轉量產 (Product Change Notice)
    ECN = "ECN"   # 產品工程變更 (Engineering Change Notice)


class PCNFormStatus(str, enum.Enum):
    DRAFT               = "DRAFT"               # 草稿（工程師填寫）
    PENDING_QC          = "PENDING_QC"          # 待品保填寫 SIP 檢表
    PENDING_PRODUCTION  = "PENDING_PRODUCTION"  # 待產線主管填寫 SOP
    PENDING_BU_APPROVAL = "PENDING_BU_APPROVAL" # 待 BU Head 審核
    APPROVED            = "APPROVED"            # 核准（已通知相關單位）
    RETURNED            = "RETURNED"            # 退回
    CLOSED              = "CLOSED"              # 結案


# ────────────────────────────────────────────
# PCNForm 主表
# ────────────────────────────────────────────

class PCNForm(Base):
    __tablename__ = "pcn_forms"

    id            = Column(Integer, primary_key=True, index=True)
    form_id       = Column(String(30), unique=True, nullable=False)   # PCN-20260413-001
    type          = Column(Enum(PCNType), default=PCNType.PCN, nullable=False)
    status        = Column(Enum(PCNFormStatus), default=PCNFormStatus.DRAFT, nullable=False)
    bu            = Column(String(30), nullable=True)                  # 事業部（儲能/消費性）

    # 產品資訊
    product_name        = Column(String(200), nullable=False)    # 產品名稱
    product_model       = Column(String(100), nullable=True)     # 產品型號
    change_description  = Column(Text, nullable=False)           # 變更說明
    change_reason       = Column(Text, nullable=True)            # 變更原因
    effective_date      = Column(String(20), nullable=True)      # 預計生效日期

    # 負責人指派
    created_by          = Column(Integer, ForeignKey("users.id"), nullable=False)
    assigned_qc_id      = Column(Integer, ForeignKey("users.id"), nullable=True)   # 指定品保
    assigned_prod_mgr_id= Column(Integer, ForeignKey("users.id"), nullable=True)   # 指定產線主管

    # 各階段意見
    qc_comment          = Column(Text, nullable=True)   # 品保意見
    prod_comment        = Column(Text, nullable=True)   # 產線主管意見

    created_at  = Column(DateTime, default=datetime.utcnow)
    updated_at  = Column(DateTime, default=datetime.utcnow, onupdate=datetime.utcnow)

    # ── 關聯 ──
    creator         = relationship("User", foreign_keys=[created_by])
    assigned_qc     = relationship("User", foreign_keys=[assigned_qc_id])
    assigned_prod_mgr = relationship("User", foreign_keys=[assigned_prod_mgr_id])
    documents       = relationship(
        "PCNDocument", back_populates="form",
        cascade="all, delete-orphan",
        order_by="PCNDocument.uploaded_at"
    )
    approvals       = relationship(
        "PCNApproval", back_populates="form",
        cascade="all, delete-orphan",
        order_by="PCNApproval.created_at"
    )


# ────────────────────────────────────────────
# PCNDocument 附件
# ────────────────────────────────────────────

class PCNDocument(Base):
    __tablename__ = "pcn_documents"

    id           = Column(Integer, primary_key=True, index=True)
    form_id_fk   = Column(Integer, ForeignKey("pcn_forms.id"), nullable=False)
    filename     = Column(String(255), nullable=False)      # 存檔名稱（uuid）
    original_name= Column(String(255), nullable=False)      # 原始檔名
    category     = Column(String(50), nullable=True)        # 圖面/SIP檢表/作業SOP/包裝SOP/其它
    uploaded_by  = Column(Integer, ForeignKey("users.id"), nullable=True)
    uploaded_at  = Column(DateTime, default=datetime.utcnow)

    form         = relationship("PCNForm", back_populates="documents")
    uploader     = relationship("User", foreign_keys=[uploaded_by])


# ────────────────────────────────────────────
# PCNApproval 簽核紀錄
# ────────────────────────────────────────────

class PCNApproval(Base):
    __tablename__ = "pcn_approvals"

    id           = Column(Integer, primary_key=True, index=True)
    form_id_fk   = Column(Integer, ForeignKey("pcn_forms.id"), nullable=False)
    approver_id  = Column(Integer, ForeignKey("users.id"), nullable=False)
    action       = Column(String(20), nullable=False)  # SUBMIT/QC_DONE/PROD_DONE/APPROVE/REJECT
    comment      = Column(Text, nullable=True)
    from_status  = Column(String(50), nullable=True)
    to_status    = Column(String(50), nullable=True)
    created_at   = Column(DateTime, default=datetime.utcnow)

    form         = relationship("PCNForm", back_populates="approvals")
    approver     = relationship("User", foreign_keys=[approver_id])
