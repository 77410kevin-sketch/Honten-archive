from sqlalchemy import Column, Integer, String, Boolean, Enum, DateTime, Float, Text, ForeignKey
from sqlalchemy.orm import relationship
from datetime import datetime
import enum
from app.database import Base


class NPIStage(str, enum.Enum):
    RFQ = "RFQ"   # 外部詢價評估
    NPI = "NPI"   # 客戶確定開發


class NPIFormStatus(str, enum.Enum):
    # RFQ 流程
    DRAFT                = "DRAFT"                # 業務草稿
    ENG_DISPATCH         = "ENG_DISPATCH"         # 工程排製程並派發供應商
    QUOTING              = "QUOTING"              # 供應商報價中（採購收集）
    QUOTES_COLLECTED     = "QUOTES_COLLECTED"     # 採購宣告收齊 → 交業務試算
    PENDING_QUOTE_BU     = "PENDING_QUOTE_BU"     # 業務完成試算 → 送 BU 審核報價/利潤
    QUOTE_APPROVED       = "QUOTE_APPROVED"       # BU 核准 → 業務可發送客戶
    RFQ_DONE             = "RFQ_DONE"             # 客戶報價已發送（等客戶決定）
    # NPI 流程
    NPI_STARTED          = "NPI_STARTED"          # 客戶確定開發，工程選供應商
    NPI_PENDING_BU       = "NPI_PENDING_BU"       # 待 BU 核准成案
    NPI_PENDING_PURCHASE = "NPI_PENDING_PURCHASE" # 待採購議價
    # 通用
    RETURNED             = "RETURNED"             # 退回
    CLOSED               = "CLOSED"               # 結案


class NPIForm(Base):
    __tablename__ = "npi_forms"

    id               = Column(Integer, primary_key=True, index=True)
    form_id          = Column(String(30), unique=True, nullable=False)  # NPI-YYYYMMDD-NNN
    stage            = Column(Enum(NPIStage), default=NPIStage.RFQ, nullable=False)
    status           = Column(Enum(NPIFormStatus), default=NPIFormStatus.DRAFT, nullable=False)

    # 客戶/產品
    customer_name    = Column(String(200), nullable=False)
    customer_contact = Column(String(100), nullable=True)
    customer_email   = Column(String(200), nullable=True)
    product_name     = Column(String(200), nullable=False)
    product_model    = Column(String(100), nullable=True)
    spec_summary     = Column(Text, nullable=True)
    target_price     = Column(Float, nullable=True)
    annual_qty       = Column(Integer, nullable=True)
    rfq_due_date     = Column(String(20), nullable=True)  # 客戶回覆期限
    bu               = Column(String(50), nullable=True)

    # 業務 / 工程補充
    sales_note       = Column(Text, nullable=True)   # 業務補充資訊
    eng_process_note = Column(Text, nullable=True)   # 工程排製程評估內容
    cost_analysis_note = Column(Text, nullable=True) # 業務成本分析摘要
    quote_cost_data  = Column(Text, nullable=True)   # 業務試算 JSON（明細/管銷/毛利/建議售價）
    quoted_unit_price= Column(Float, nullable=True)  # 業務決定給客戶的報價單價
    bu_quote_note    = Column(Text, nullable=True)   # BU 對報價的核准/退回評語
    bargain_data     = Column(Text, nullable=True)   # 採購議價覆寫 JSON（prices/tooling/note）
    selected_quote_supplier_id = Column(Integer, ForeignKey("suppliers.id"), nullable=True)

    # NPI 階段（ERP 模具請購單）
    erp_req_no       = Column(String(50), nullable=True)
    erp_req_data     = Column(Text, nullable=True)   # JSON 快照
    mould_cost_est   = Column(Float, nullable=True)  # 工程估算成本
    mould_cost_final = Column(Float, nullable=True)  # 採購議價後實際成本
    purchase_note    = Column(Text, nullable=True)   # 採購議價備註
    t1_plan_data     = Column(Text, nullable=True)   # 每張圖客戶 T1 試模日期+備註 JSON
    eng_process_data = Column(Text, nullable=True)   # NPI 工程填寫：{process_name:{part_no, need_routing}} JSON
    nas_folder       = Column(String(300), nullable=True)

    # 退回控制
    reject_to        = Column(String(50), nullable=True)

    # 關聯
    created_by       = Column(Integer, ForeignKey("users.id"), nullable=False)
    assigned_eng_id  = Column(Integer, ForeignKey("users.id"), nullable=True)
    created_at       = Column(DateTime, default=datetime.utcnow)
    updated_at       = Column(DateTime, default=datetime.utcnow, onupdate=datetime.utcnow)

    creator          = relationship("User", foreign_keys=[created_by])
    assigned_eng     = relationship("User", foreign_keys=[assigned_eng_id])
    selected_quote_supplier = relationship("Supplier", foreign_keys=[selected_quote_supplier_id])

    invites          = relationship("NPISupplierInvite", back_populates="form",
                                    cascade="all, delete-orphan",
                                    order_by="NPISupplierInvite.invited_at")
    documents        = relationship("NPIDocument", back_populates="form",
                                    cascade="all, delete-orphan",
                                    order_by="NPIDocument.uploaded_at")
    approvals        = relationship("NPIApproval", back_populates="form",
                                    cascade="all, delete-orphan",
                                    order_by="NPIApproval.created_at")


class NPISupplierInvite(Base):
    """本單派發給哪些供應商 + 各家報價狀態"""
    __tablename__ = "npi_supplier_invites"

    id             = Column(Integer, primary_key=True, index=True)
    form_id_fk     = Column(Integer, ForeignKey("npi_forms.id"), nullable=False)
    supplier_id    = Column(Integer, ForeignKey("suppliers.id"), nullable=False)

    # 工程派發時填寫：每列 = 一個製程 / 一家供應商
    process_name       = Column(String(100), nullable=True)   # 例：CNC 加工、表面處理
    material           = Column(String(100), nullable=True)   # 例：SUS304、鋁 6061
    qty                = Column(Integer, nullable=True)       # 派發數量
    expected_lead_days = Column(Integer, nullable=True)       # 工程期望工作天
    drawing_doc_id     = Column(Integer, ForeignKey("npi_documents.id"), nullable=True)  # 對應圖面（null=共用/所有圖）

    invited_at     = Column(DateTime, default=datetime.utcnow)
    first_sent_at  = Column(DateTime, nullable=True)    # 第一次寄信時間
    last_reminder_at = Column(DateTime, nullable=True)  # 最近一次跟催時間
    reminder_count = Column(Integer, default=0)

    replied_at     = Column(DateTime, nullable=True)
    quote_amount   = Column(Float, nullable=True)
    tooling_cost   = Column(Float, nullable=True)       # 模治具費用（獨立計價，不計入單價）
    lead_time_days = Column(Integer, nullable=True)
    quote_comment  = Column(Text, nullable=True)
    is_selected    = Column(Boolean, default=False)     # NPI 最終選用
    tier_data      = Column(Text, nullable=True)        # 階梯式 MOQ JSON：[{"qty":100,"price":500},...]

    form           = relationship("NPIForm", back_populates="invites")
    supplier       = relationship("Supplier", foreign_keys=[supplier_id])
    drawing        = relationship("NPIDocument", foreign_keys=[drawing_doc_id])


class NPIDocument(Base):
    __tablename__ = "npi_documents"

    id            = Column(Integer, primary_key=True, index=True)
    form_id_fk    = Column(Integer, ForeignKey("npi_forms.id"), nullable=False)
    invite_id_fk  = Column(Integer, ForeignKey("npi_supplier_invites.id"), nullable=True)
    filename      = Column(String(255), nullable=False)
    original_name = Column(String(255), nullable=False)
    category      = Column(String(50), nullable=True)
    uploaded_by   = Column(Integer, ForeignKey("users.id"), nullable=True)
    uploaded_at   = Column(DateTime, default=datetime.utcnow)

    form     = relationship("NPIForm", back_populates="documents")
    uploader = relationship("User", foreign_keys=[uploaded_by])


class NPIApproval(Base):
    __tablename__ = "npi_approvals"

    id            = Column(Integer, primary_key=True, index=True)
    form_id_fk    = Column(Integer, ForeignKey("npi_forms.id"), nullable=False)
    approver_id   = Column(Integer, ForeignKey("users.id"), nullable=False)
    action        = Column(String(30), nullable=False)
    comment       = Column(Text, nullable=True)
    reject_target = Column(String(50), nullable=True)
    from_status   = Column(String(50), nullable=True)
    to_status     = Column(String(50), nullable=True)
    created_at    = Column(DateTime, default=datetime.utcnow)

    form     = relationship("NPIForm", back_populates="approvals")
    approver = relationship("User", foreign_keys=[approver_id])
