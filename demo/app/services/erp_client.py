"""ERP 連接口 — SQL Server 直連（鼎新 HTE2026）

.env 設定：
    ERP_BACKEND=sqlserver
    ERP_SERVER=192.168.0.201
    ERP_PORT=1433
    ERP_DATABASE=HTE2026
    ERP_USER=ht_sys
    ERP_PASSWORD=***
    ERP_ODBC_DRIVER=SQL Server   # 或 ODBC Driver 17 for SQL Server
"""
from __future__ import annotations
import os
import logging
from dataclasses import dataclass, asdict
from typing import Protocol

logger = logging.getLogger(__name__)


# ── DTOs ──────────────────────────────────────────
@dataclass
class ERPCustomer:
    erp_code:  str
    name:      str
    contact:   str | None = None
    email:     str | None = None
    phone:     str | None = None
    address:   str | None = None
    bu:        str | None = None
    tax_id:    str | None = None
    is_active: bool = True

    def as_dict(self) -> dict:
        return asdict(self)


@dataclass
class ERPSupplier:
    erp_code:  str
    name:      str
    type:      str = "外部"
    contact:   str | None = None
    email:     str | None = None
    phone:     str | None = None
    tax_id:    str | None = None
    is_active: bool = True

    def as_dict(self) -> dict:
        return asdict(self)


@dataclass
class ERPProcess:
    code:                 str
    name:                 str
    category:             str | None = None
    default_supplier_ids: list[str] | None = None

    def as_dict(self) -> dict:
        return asdict(self)


# ── Backend Protocol ───────────────────────────────
class ERPBackend(Protocol):
    def fetch_customers(self) -> list[ERPCustomer]: ...
    def fetch_suppliers(self) -> list[ERPSupplier]: ...
    def fetch_processes(self) -> list[ERPProcess]: ...
    def is_connected(self) -> bool: ...


# ── SQL Server 實作 ────────────────────────────────
class _SQLServerBackend:
    def __init__(self):
        driver   = os.getenv("ERP_ODBC_DRIVER", "SQL Server")
        server   = os.getenv("ERP_SERVER", "192.168.0.201")
        port     = os.getenv("ERP_PORT", "1433")
        database = os.getenv("ERP_DATABASE", "HTE2026")
        user     = os.getenv("ERP_USER", "ht_sys")
        password = os.getenv("ERP_PASSWORD", "")
        self._conn_str = (
            f"DRIVER={{{driver}}};"
            f"SERVER={server},{port};"
            f"DATABASE={database};"
            f"UID={user};PWD={password};"
            "TrustServerCertificate=yes;timeout=10;"
        )
        self._ok: bool | None = None  # 快取連線測試結果

    def _connect(self):
        import pyodbc
        return pyodbc.connect(self._conn_str, timeout=10)

    def is_connected(self) -> bool:
        if self._ok is not None:
            return self._ok
        try:
            conn = self._connect()
            conn.close()
            self._ok = True
        except Exception as e:
            logger.warning(f"ERP 連線測試失敗: {e}")
            self._ok = False
        return self._ok

    def _query(self, sql: str) -> list[dict]:
        conn = self._connect()
        try:
            cur = conn.cursor()
            cur.execute(sql)
            cols = [c[0] for c in cur.description]
            rows = []
            for r in cur.fetchall():
                row = {}
                for k, v in zip(cols, r):
                    if isinstance(v, str):
                        v = v.strip()
                    row[k] = v
                rows.append(row)
            return rows
        finally:
            conn.close()

    def fetch_customers(self) -> list[ERPCustomer]:
        try:
            rows = self._query("""
                SELECT
                    customer_code       AS erp_code,
                    customer_name       AS name,
                    NULL                AS contact,
                    NULL                AS email,
                    NULL                AS phone,
                    NULL                AS address,
                    NULL                AS bu,
                    NULL                AS tax_id,
                    1                   AS is_active
                FROM v_ht_customer_order
                GROUP BY customer_code, customer_name
                ORDER BY customer_name
            """)
            return [ERPCustomer(**_safe(r, ERPCustomer)) for r in rows]
        except Exception as e:
            logger.error(f"fetch_customers 失敗: {e}")
            return _stub_customers()

    def fetch_suppliers(self) -> list[ERPSupplier]:
        try:
            rows = self._query("""
                SELECT
                    vendor_code AS erp_code,
                    vendor_name AS name,
                    '外部'      AS type,
                    NULL        AS contact,
                    NULL        AS email,
                    NULL        AS phone,
                    NULL        AS tax_id,
                    1           AS is_active
                FROM v_ht_purchase_order
                GROUP BY vendor_code, vendor_name
                ORDER BY vendor_name
            """)
            return [ERPSupplier(**_safe(r, ERPSupplier)) for r in rows]
        except Exception as e:
            logger.error(f"fetch_suppliers 失敗: {e}")
            return []

    def fetch_processes(self) -> list[ERPProcess]:
        return _stub_processes()

    def query_customer_orders(self, customer_name: str = "", days: int = 90) -> list[dict]:
        """查客戶訂單明細（NPI 建單時用）"""
        where = f"AND customer_name LIKE N'%{customer_name}%'" if customer_name else ""
        return self._query(f"""
            SELECT TOP 200
                order_no, customer_name, part_no, product_name,
                order_date, order_qty, unit_price, amount_ntd, delivery_date
            FROM v_ht_customer_order_lines
            WHERE order_date >= DATEADD(day, -{days}, GETDATE())
            {where}
            ORDER BY order_date DESC
        """)

    def query_purchase_orders(self, days: int = 30) -> list[dict]:
        return self._query(f"""
            SELECT TOP 200
                order_no, vendor_name, product_code, product_name,
                order_date, qty, unit_price, amount, delivery_date, delivered_qty
            FROM v_ht_purchase_order
            WHERE order_date >= DATEADD(day, -{days}, GETDATE())
            ORDER BY order_date DESC
        """)

    def query_manufacturing_orders(self, part_no: str = "") -> list[dict]:
        where = f"WHERE part_no = N'{part_no}'" if part_no else ""
        return self._query(f"""
            SELECT TOP 100 * FROM v_ht_manufacturing_order
            {where}
            ORDER BY order_date DESC
        """)


# ── Stub fallback ──────────────────────────────────
def _stub_customers() -> list[ERPCustomer]:
    return [
        ERPCustomer("C001", "景利（Jingli）",     "林副總", "lin@jingli.com.tw",    "02-1111-2222", "新北市", "儲能事業部",   "12345678"),
        ERPCustomer("C002", "愛爾蘭金士頓",       "Kevin",  "kevin@airkingston.ie", "+353-1-222-3333", "Dublin", "消費性事業部", "IE1234567"),
        ERPCustomer("C003", "Dynapack International Technology Corp.", "王廠長", None, None, None, "儲能事業部", None),
        ERPCustomer("C004", "瑞傳電子",           "陳經理", None,                   None,             "新竹",   "儲能事業部",   None),
    ]


def _stub_processes() -> list[ERPProcess]:
    return [
        ERPProcess("P001", "沖壓",  "機加工"),
        ERPProcess("P002", "鋁擠",  "機加工"),
        ERPProcess("P003", "NCT",   "機加工"),
        ERPProcess("P004", "CNC",   "機加工"),
        ERPProcess("P005", "噴砂",  "表面處理"),
        ERPProcess("P006", "髮線",  "表面處理"),
        ERPProcess("P007", "陽極",  "表面處理"),
        ERPProcess("P008", "烤漆",  "表面處理"),
        ERPProcess("P009", "電鍍",  "表面處理"),
    ]


def _safe(row: dict, cls) -> dict:
    """只保留 dataclass 有的欄位，避免多餘欄位炸 TypeError。"""
    import dataclasses
    keys = {f.name for f in dataclasses.fields(cls)}
    return {k: v for k, v in row.items() if k in keys}


# ── Stub 實作（開發用）────────────────────────────
class _StubBackend:
    def fetch_customers(self) -> list[ERPCustomer]: return _stub_customers()
    def fetch_suppliers(self) -> list[ERPSupplier]: return []
    def fetch_processes(self) -> list[ERPProcess]:  return _stub_processes()
    def is_connected(self) -> bool: return True


# ── 單例切換 ──────────────────────────────────────
def _select_backend() -> ERPBackend:
    if os.getenv("ERP_BACKEND", "stub").lower() == "sqlserver":
        return _SQLServerBackend()
    return _StubBackend()


_BACKEND: ERPBackend = _select_backend()


# ── 對外 API ──────────────────────────────────────
def fetch_customers_from_erp() -> list[ERPCustomer]:
    try:
        return _BACKEND.fetch_customers()
    except Exception as e:
        logger.warning(f"fetch_customers 例外: {e}")
        return []


def fetch_suppliers_from_erp() -> list[ERPSupplier]:
    try:
        return _BACKEND.fetch_suppliers()
    except Exception as e:
        logger.warning(f"fetch_suppliers 例外: {e}")
        return []


def fetch_processes_from_erp() -> list[ERPProcess]:
    try:
        return _BACKEND.fetch_processes()
    except Exception:
        return []


def erp_status() -> dict:
    backend_name = os.getenv("ERP_BACKEND", "stub").lower()
    return {
        "backend":    backend_name,
        "is_stub":    backend_name == "stub",
        "connected":  _BACKEND.is_connected(),
    }


def erp_query_customer_orders(customer_name: str = "", days: int = 90) -> list[dict]:
    if isinstance(_BACKEND, _SQLServerBackend):
        return _BACKEND.query_customer_orders(customer_name, days)
    return []


def erp_query_purchase_orders(days: int = 30) -> list[dict]:
    if isinstance(_BACKEND, _SQLServerBackend):
        return _BACKEND.query_purchase_orders(days)
    return []


def erp_query_manufacturing_orders(part_no: str = "") -> list[dict]:
    if isinstance(_BACKEND, _SQLServerBackend):
        return _BACKEND.query_manufacturing_orders(part_no)
    return []
