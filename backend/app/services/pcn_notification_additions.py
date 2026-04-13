"""
PCN/ECN 通知函式 — 加入 notification.py 的內容
請將以下函式貼入 app/services/notification.py 現有檔案末尾
"""

# ── 新增這段到 notification.py 末尾 ─────────────────────────────────────────

"""
async def notify_pcn_submitted(db, form):
    \"\"\"工程師送審 → 通知指定品保（或所有品保）\"\"\"
    if form.assigned_qc_id:
        recipient = await db.get(User, form.assigned_qc_id)
        recipients = [recipient] if recipient else []
    else:
        result = await db.execute(select(User).where(User.role == Role.QC, User.is_active == True))
        recipients = result.scalars().all()

    type_label = "PCN（開發轉量產）" if form.type.value == "PCN" else "ECN（產品工程變更）"
    msg = (
        f"【待品保填寫 SIP 檢表】\\n"
        f"📋 單號：{form.form_id}\\n"
        f"🔖 類型：{type_label}\\n"
        f"📦 產品：{form.product_name}\\n"
        f"請登入系統上傳 SIP 檢表並完成品保作業。"
    )
    await _notify_line_no_form(db, recipients, msg)


async def notify_pcn_qc_done(db, form):
    \"\"\"品保完成 → 通知指定產線主管（或所有產線主管）\"\"\"
    if form.assigned_prod_mgr_id:
        recipient = await db.get(User, form.assigned_prod_mgr_id)
        recipients = [recipient] if recipient else []
    else:
        result = await db.execute(select(User).where(User.role == Role.PROD_MGR, User.is_active == True))
        recipients = result.scalars().all()

    msg = (
        f"【待產線主管填寫 SOP】\\n"
        f"📋 單號：{form.form_id}\\n"
        f"📦 產品：{form.product_name}\\n"
        f"品保作業已完成，請上傳作業 SOP 與包裝 SOP。"
    )
    await _notify_line_no_form(db, recipients, msg)


async def notify_pcn_prod_done(db, form):
    \"\"\"產線完成 → 通知 BU Head（依 BU）\"\"\"
    q = select(User).where(User.role == Role.BU, User.is_active == True)
    if form.bu:
        q = q.where(User.bu == form.bu)
    result = await db.execute(q)
    bu_heads = result.scalars().all()

    msg = (
        f"【待 BU 主管審核 PCN/ECN】\\n"
        f"📋 單號：{form.form_id}\\n"
        f"📦 產品：{form.product_name}\\n"
        f"品保及產線作業均已完成，請審核。"
    )
    await _notify_line_no_form(db, bu_heads, msg)


async def notify_pcn_approved(db, form):
    \"\"\"BU Head 核准 → LINE 通知所有相關人員（工程師、品保、產線主管）取代 Mail\"\"\"
    recipient_ids = set()
    recipient_ids.add(form.created_by)
    if form.assigned_qc_id:
        recipient_ids.add(form.assigned_qc_id)
    if form.assigned_prod_mgr_id:
        recipient_ids.add(form.assigned_prod_mgr_id)

    recipients = []
    for uid in recipient_ids:
        u = await db.get(User, uid)
        if u:
            recipients.append(u)

    # 另外 C.C. 工程主管
    cc_result = await db.execute(select(User).where(User.role == Role.ENG_MGR, User.is_active == True))
    recipients += cc_result.scalars().all()

    type_label = "PCN（開發轉量產）" if form.type.value == "PCN" else "ECN（產品工程變更）"
    msg = (
        f"【✅ PCN/ECN 已核准】\\n"
        f"📋 單號：{form.form_id}\\n"
        f"🔖 類型：{type_label}\\n"
        f"📦 產品：{form.product_name}\\n"
        f"{'📅 生效日期：' + form.effective_date if form.effective_date else ''}\\n"
        f"BU 主管已核准，請各單位依核准文件執行。"
    )
    await _notify_line_no_form(db, recipients, msg)


async def notify_pcn_rejected(db, form):
    \"\"\"BU Head 退回 → 通知工程師（建單者）\"\"\"
    creator = await db.get(User, form.created_by)
    recipients = [creator] if creator else []

    msg = (
        f"【⚠️ PCN/ECN 已退回】\\n"
        f"📋 單號：{form.form_id}\\n"
        f"📦 產品：{form.product_name}\\n"
        f"BU 主管已退回，請修改後重新送審。"
    )
    await _notify_line_no_form(db, recipients, msg)
"""
