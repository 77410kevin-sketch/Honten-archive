"""
SQLite 資料庫模組 — 儲存已生成的量測檢表
"""
import sqlite3
import json
import os

# 存放在 HonTen data/ 目錄下
DB_PATH = os.path.join(
    os.path.dirname(os.path.abspath(__file__)),
    "..", "..", "data", "checklists.db"
)

def init_db():
    os.makedirs(os.path.dirname(DB_PATH), exist_ok=True)
    with sqlite3.connect(DB_PATH) as conn:
        conn.execute("""
            CREATE TABLE IF NOT EXISTS checklists (
                id           INTEGER PRIMARY KEY AUTOINCREMENT,
                part_name    TEXT NOT NULL,
                drawing_no   TEXT,
                internal_no  TEXT,
                created_at   TEXT DEFAULT (datetime('now','localtime')),
                dimensions_json TEXT NOT NULL,
                tools_json   TEXT,
                preview_b64  TEXT
            )
        """)
        conn.commit()

def save(part_name, drawing_no, internal_no, dimensions, tools, preview_b64):
    with sqlite3.connect(DB_PATH) as conn:
        cur = conn.execute(
            """INSERT INTO checklists
               (part_name, drawing_no, internal_no, dimensions_json, tools_json, preview_b64)
               VALUES (?,?,?,?,?,?)""",
            (part_name, drawing_no, internal_no,
             json.dumps(dimensions, ensure_ascii=False),
             json.dumps(tools, ensure_ascii=False),
             preview_b64),
        )
        conn.commit()
        return cur.lastrowid

def list_all():
    with sqlite3.connect(DB_PATH) as conn:
        conn.row_factory = sqlite3.Row
        rows = conn.execute(
            "SELECT id, part_name, drawing_no, internal_no, created_at, preview_b64 "
            "FROM checklists ORDER BY created_at DESC"
        ).fetchall()
        return [dict(r) for r in rows]

def get(checklist_id: int):
    with sqlite3.connect(DB_PATH) as conn:
        conn.row_factory = sqlite3.Row
        row = conn.execute(
            "SELECT * FROM checklists WHERE id=?", (checklist_id,)
        ).fetchone()
        if not row:
            return None
        r = dict(row)
        r["dimensions"] = json.loads(r["dimensions_json"])
        r["tools"] = json.loads(r["tools_json"] or "{}")
        return r

def delete(checklist_id: int):
    with sqlite3.connect(DB_PATH) as conn:
        conn.execute("DELETE FROM checklists WHERE id=?", (checklist_id,))
        conn.commit()

def count():
    with sqlite3.connect(DB_PATH) as conn:
        return conn.execute("SELECT COUNT(*) FROM checklists").fetchone()[0]
