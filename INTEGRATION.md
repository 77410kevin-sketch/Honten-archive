# PCN/ECN 模組整合說明

> 本文件說明如何將 PCN/ECN 模組整合進主系統。
> 開發完成日期：2026-04-13

---

## 一、新增檔案（直接複製）

| 來源路徑 | 目的路徑 | 說明 |
|---|---|---|
| `backend/app/models/pcn_form.py` | 同路徑 | PCN/ECN 資料模型 |
| `backend/app/routes/pcn_forms.py` | 同路徑 | PCN/ECN 路由 |
| `backend/app/templates/pcn_forms/` | 同路徑（整個資料夾） | 4個 HTML 模板 |

---

## 二、修改現有檔案

### 1. `app/models/user.py` — 新增角色

在 `Role` enum 中加入兩個新角色：

```python
class Role(str, enum.Enum):
    ADMIN       = "admin"
    SALES       = "sales"
    SALES_SENIOR= "sales_senior"
    BU          = "bu"
    ENGINEER    = "engineer"
    ENG_MGR     = "eng_mgr"
    PRODUCTION  = "production"
    PURCHASE    = "purchase"
    ASSISTANT   = "assistant"
    # ── 新增 PCN/ECN 角色 ──
    QC          = "qc"        # 品保人員
    PROD_MGR    = "prod_mgr"  # 產線主管
```

> ⚠ 修改 Role enum 後**必須重啟 uvicorn**，SQLite 才能識別新值。

---

### 2. `app/models/__init__.py` — 加入 import

```python
from app.models.pcn_form import *
```

---

### 3. `app/main.py` — 註冊路由

```python
from app.routes import pcn_forms

# 在 include_router 區塊加入（順序不影響）：
app.include_router(pcn_forms.router)
```

---

### 4. `app/templates/base.html` — 新增側欄連結

在側欄導覽的工程相關區塊加入（桌機版 + 手機版各一處）：

```html
{% if user.role.value in ('engineer', 'qc', 'prod_mgr', 'bu', 'admin') %}
<a href="/pcn-forms/" class="nav-link {% if '/pcn-forms' in request.url.path %}active{% endif %}">
  <i class="bi bi-arrow-repeat me-2"></i>PCN/ECN 管理
</a>
{% endif %}
```

---

### 5. `app/services/notification.py` — 新增 PCN 通知函式

將 `pcn_notification_additions.py` 中的函式貼入 `notification.py` 末尾：

```python
async def notify_pcn_submitted(db, form): ...
async def notify_pcn_qc_done(db, form): ...
async def notify_pcn_prod_done(db, form): ...
async def notify_pcn_approved(db, form): ...
async def notify_pcn_rejected(db, form): ...
```

---

### 6. `app/routes/dashboard.py` — 新增待辦統計

在各角色的待辦查詢區塊加入：

```python
from app.models.pcn_form import PCNForm, PCNFormStatus

# 品保待辦
if role in (Role.QC, Role.ADMIN):
    r = await db.execute(
        select(func.count()).where(PCNForm.status == PCNFormStatus.PENDING_QC)
    )
    stats["pcn_pending_qc"] = r.scalar() or 0

# 產線主管待辦
if role in (Role.PROD_MGR, Role.ADMIN):
    r = await db.execute(
        select(func.count()).where(PCNForm.status == PCNFormStatus.PENDING_PRODUCTION)
    )
    stats["pcn_pending_prod"] = r.scalar() or 0

# BU Head 待辦
if role in (Role.BU, Role.ADMIN):
    r = await db.execute(
        select(func.count()).where(PCNForm.status == PCNFormStatus.PENDING_BU_APPROVAL)
    )
    stats["pcn_pending_bu"] = r.scalar() or 0
```

在 `dashboard.html` 加入對應卡片：

```html
{% if stats.get('pcn_pending_qc') is not none %}
<div class="col-md-3">
  <a href="/pcn-forms/?status=PENDING_QC" class="text-decoration-none">
    <div class="card border-primary shadow-sm">
      <div class="card-body text-center">
        <div class="fs-2 fw-bold text-primary">{{ stats.pcn_pending_qc }}</div>
        <div class="text-muted small">PCN/ECN 待品保</div>
      </div>
    </div>
  </a>
</div>
{% endif %}
```

---

### 7. `app/routes/admin.py` — 新增帳號管理選項

在使用者管理頁面的角色下拉選單中加入：

```html
<option value="qc">品保人員</option>
<option value="prod_mgr">產線主管</option>
```

---

## 三、資料庫遷移

PCN/ECN 新表（`pcn_forms`、`pcn_documents`、`pcn_approvals`）會在 uvicorn 啟動時由 `create_all` 自動建立，**不需要手動遷移**。

---

## 四、測試帳號建議

整合後請在管理員介面新增以下測試帳號：

| 帳號 | 角色 | 密碼 |
|---|---|---|
| qc01 | qc（品保） | ht1234 |
| prodmgr01 | prod_mgr（產線主管） | ht1234 |

---

## 五、完整流程驗證清單

- [ ] 工程師登入 → 建立 PCN → 上傳圖面 → 送審
- [ ] 品保登入 → 看到待辦 → 上傳 SIP 檢表 → 完成
- [ ] 產線主管登入 → 上傳作業SOP + 包裝SOP → 完成
- [ ] BU Head 登入 → 審核 → 核准（確認 LINE 通知送出）
- [ ] BU Head 退回 → 工程師修改 → 重新送審
- [ ] 核准後工程師結案
- [ ] 儀表板待辦數字正確顯示

---

## 六、附件存放路徑

```
uploads/
├── pcn_{form.id}/     ← PCN/ECN 附件（圖面/SIP/SOP等）
├── {form.id}/         ← 業務需求單附件（既有）
└── eng_{form.id}/     ← 製程單附件（既有）
```

---

## 七、PCN/ECN 流程圖

```
工程師建立
    │ 上傳圖面（必要）
    ▼
DRAFT（草稿）
    │ 送審
    ▼
PENDING_QC（待品保）
    │ 品保上傳 SIP 檢表（必要）→ 完成
    ▼
PENDING_PRODUCTION（待產線主管）
    │ 產線上傳作業SOP + 包裝SOP（必要）→ 完成
    ▼
PENDING_BU_APPROVAL（待 BU Head 審核）
    │
    ├─ 核准 → APPROVED（LINE 推播通知所有相關人員）
    │                │ 工程師結案
    │                ▼
    │             CLOSED
    │
    └─ 退回 → RETURNED → 工程師修改 → 重新送審
```
