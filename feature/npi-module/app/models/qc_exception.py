from sqlalchemy import Column, Integer, String, Enum, DateTime, Text, ForeignKey, Float, Boolean
from sqlalchemy.orm import relationship
from datetime import datetime
import enum
from app.database import Base


class QCExceptionStatus(str, enum.Enum):
    DRAFT                = "DRAFT"                # 品保草稿（IPC 異常資訊填寫中）
    PENDING_DISPOSITION  = "PENDING_DISPOSITION"  # 待品保下處理判斷（退貨/實驗/特採）
    PENDING_RCA          = "PENDING_RCA"          # 待 Mail 通知 + 根因分析
    PENDING_IMPROVEMENT  = "PENDING_IMPROVEMENT"  # 待制定長期改善方案（圖面/SOP/SIP/ECN）
    LINKED_ECN           = "LINKED_ECN"           # 已開 ECN，等對應 ECN 結案
    CLOSED               = "CLOSED"               # 結案


class QCDisposition(str, enum.Enum):
    RETURN_TO_SUPPLIER  = "RETURN_TO_SUPPLIER"   # 退貨
    LAB_TEST            = "LAB_TEST"             # 實驗測試
    SPECIAL_ACCEPT      = "SPECIAL_ACCEPT"       # 特採允收


class QCExceptionStage(str, enum.Enum):
    """異常發生工段"""
    IQC        = "IQC"        # 進料檢驗
    IPQC       = "IPQC"       # 製程檢驗
    OQC        = "OQC"        # 出貨檢驗
    INSPECTION = "INSPECTION" # 品檢
    LASER      = "LASER"      # 雷雕
    CNC        = "CNC"        # CNC
    ASSEMBLY   = "ASSEMBLY"   # 組裝
    OTHER      = "OTHER"


class QCDocType(str, enum.Enum):
    """單號類型 — 進貨單號 / 製程單號 / 出貨 D/C"""
    RECEIVE  = "RECEIVE"   # 進貨單號
    PROCESS  = "PROCESS"   # 製程單號
    SHIP_DC  = "SHIP_DC"   # 出貨 D/C


class QCEventDateType(str, enum.Enum):
    """日期類型 — 進貨/生產/出貨/客訴"""
    RECEIVE   = "RECEIVE"   # 進貨日期
    PRODUCE   = "PRODUCE"   # 生產日期
    SHIP      = "SHIP"      # 出貨日期
    COMPLAINT = "COMPLAINT" # 客訴日期


class QCException(Base):
    __tablename__ = "qc_exceptions"

    id                = Column(Integer, primary_key=True, index=True)
    form_id           = Column(String(30), unique=True, nullable=False)  # NCR-YYYYMMDD-NNN
    status            = Column(Enum(QCExceptionStatus),
                               default=QCExceptionStatus.DRAFT, nullable=False)

    # ── IPC 異常資訊（業務首頁示例）─────────────────
    part_no           = Column(String(80),  nullable=False)   # 品號 KS04P
    # 單號（類型 + 號碼）
    doc_type          = Column(Enum(QCDocType),
                               default=QCDocType.RECEIVE, nullable=True)
    receive_doc_no    = Column(String(80),  nullable=True)    # 單號號碼
    lot_no            = Column(String(80),  nullable=True)    # 批號（保留 DB 欄位但 UI 已隱藏）
    # 日期（類型 + 值）
    event_date_type   = Column(Enum(QCEventDateType),
                               default=QCEventDateType.RECEIVE, nullable=True)
    receive_date      = Column(String(20),  nullable=True)    # 日期值
    stage             = Column(Enum(QCExceptionStage),
                               default=QCExceptionStage.IQC, nullable=False)
    supplier_name     = Column(String(120), nullable=True)    # 廠商 展倚
    receive_qty       = Column(Integer,     nullable=True)    # 數量
    defect_cause      = Column(Text,        nullable=False)   # 異常原因 總長過長 32.00+-0.1
    measurement_data  = Column(Text,        nullable=True)    # 量測數據 32.11~32.15
    defect_qty        = Column(Integer,     nullable=True)    # 不良數量 40
    sample_qty        = Column(Integer,     nullable=True)    # 抽樣數量 315
    defect_rate       = Column(Float,       nullable=True)    # 不良率 0.126

    # ── 品保處理判斷 ────────────────────────────────
    disposition       = Column(Enum(QCDisposition), nullable=True)
    disposition_note  = Column(Text, nullable=True)            # 處理判斷說明
    disposition_at    = Column(DateTime, nullable=True)
    disposition_by    = Column(Integer, ForeignKey("users.id"), nullable=True)

    # ── Mail 通知 + 根因分析 ────────────────────────
    notify_mail_to    = Column(Text, nullable=True)            # CSV 收件人 email
    notify_mail_cc    = Column(Text, nullable=True)            # CSV cc
    notify_sent_at    = Column(DateTime, nullable=True)
    root_cause        = Column(Text, nullable=True)            # 根因分析

    # ── 長期改善方案 + ECN 綁定 ─────────────────────
    need_drawing_rev  = Column(Boolean, default=False)         # 需修訂圖面
    need_sop_rev      = Column(Boolean, default=False)         # 需修訂 SOP
    need_sip_rev      = Column(Boolean, default=False)         # 需修訂 SIP
    improvement_plan  = Column(Text, nullable=True)            # 長期改善內容
    linked_ecn_form_id = Column(Integer, ForeignKey("pcn_forms.id"), nullable=True)  # 對應 ECN

    # ── 系統欄位 ────────────────────────────────────
    reject_to         = Column(String(50), nullable=True)
    created_by        = Column(Integer, ForeignKey("users.id"), nullable=False)
    assigned_qc_id    = Column(Integer, ForeignKey("users.id"), nullable=True)
    created_at        = Column(DateTime, default=datetime.utcnow)
    updated_at        = Column(DateTime, default=datetime.utcnow, onupdate=datetime.utcnow)

    creator      = relationship("User", foreign_keys=[created_by])
    assigned_qc  = relationship("User", foreign_keys=[assigned_qc_id])
    dispositioner = relationship("User", foreign_keys=[disposition_by])
    linked_ecn   = relationship("PCNForm", foreign_keys=[linked_ecn_form_id])

    documents    = relationship("QCExceptionDocument", back_populates="form",
                                cascade="all, delete-orphan",
                                order_by="QCExceptionDocument.uploaded_at")
    approvals    = relationship("QCExceptionApproval", back_populates="form",
                                cascade="all, delete-orphan",
                                order_by="QCExceptionApproval.created_at")


class QCExceptionDocument(Base):
    __tablename__ = "qc_exception_documents"

    id            = Column(Integer, primary_key=True, index=True)
    form_id_fk    = Column(Integer, ForeignKey("qc_exceptions.id"), nullable=False)
    filename      = Column(String(255), nullable=False)
    original_name = Column(String(255), nullable=False)
    category      = Column(String(50), nullable=True)   # 異常照片/實驗報告/圖面/其它
    uploaded_by   = Column(Integer, ForeignKey("users.id"), nullable=True)
    uploaded_at   = Column(DateTime, default=datetime.utcnow)

    form     = relationship("QCException", back_populates="documents")
    uploader = relationship("User", foreign_keys=[uploaded_by])


class QCExceptionApproval(Base):
    __tablename__ = "qc_exception_approvals"

    id            = Column(Integer, primary_key=True, index=True)
    form_id_fk    = Column(Integer, ForeignKey("qc_exceptions.id"), nullable=False)
    approver_id   = Column(Integer, ForeignKey("users.id"), nullable=False)
    action        = Column(String(30), nullable=False)
    comment       = Column(Text, nullable=True)
    reject_target = Column(String(50), nullable=True)
    from_status   = Column(String(50), nullable=True)
    to_status     = Column(String(50), nullable=True)
    created_at    = Column(DateTime, default=datetime.utcnow)

    form     = relationship("QCException", back_populates="approvals")
    approver = relationship("User", foreign_keys=[approver_id])
