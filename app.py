# -*- coding: utf-8 -*-
"""
تطبيق سندات استلام المواد الغذائية - Streamlit + SQLite
التشغيل:  streamlit run app.py
"""
import base64
import hashlib
import hmac
import html as htmllib
import io
import re
import os
import secrets
import shutil
import sqlite3
import subprocess
import sys
import tempfile
import time
from contextlib import contextmanager
from datetime import datetime, timedelta
from pathlib import Path

import pandas as pd
import streamlit as st
import streamlit.components.v1 as components

try:
    from st_keyup import st_keyup  # بحث فوري مع كل حرف (اختياري)
except ImportError:
    st_keyup = None

# ------------------------------------------------------------------
# الإعدادات (عدّل النصوص هنا إن احتجت)
# ------------------------------------------------------------------
DB_PATH = Path(__file__).with_name("vouchers.db")
NEW_OPTION = "➕ جديد..."
DATE_FMT = "%Y-%m-%d %H:%M:%S"

COMPANY_NAME = "شركة افاق العمل المحدودة"
DOC_TITLE = "استلام مواد غذائية"
APP_TITLE = "سندات استلام المواد الغذائية"
DOC_INTRO = "الرجاء استلام أصناف المواد الغذائية التالية"
DOC_THANKS = "شاكرين ومقدرين لكم،،،"

DEFAULT_ITEM = "سلة غذائية"
DEFAULT_UNIT = "سلة"

ITEM_COL, QTY_COL, UNIT_COL = "اسم الصنف", "الكمية", "الوحدة"

# مفاتيح الحقول -> عناوين عربية (تُستخدم في التصدير والاستيراد والقالب)
LABELS = {
    "voucher_no": "رقم السند",
    "customer": "اسم العميل (السادة)",
    "sheikh": "الشيخ",
    "phone": "الجوال",
    "project": "رقم المشروع",
    "notes": "ملاحظات",
    "regions": "المناطق",
    "item": ITEM_COL,
    "qty": QTY_COL,
    "unit": UNIT_COL,
    "created_at": "التاريخ والوقت",
}

# أسماء بديلة للتعرف التلقائي على الأعمدة عند الاستيراد
ALIASES = {
    "voucher_no": ["رقم السند", "السند", "رقم", "voucher", "voucher_no", "no", "id"],
    "customer": ["اسم العميل (السادة)", "اسم العميل", "السادة", "العميل", "customer", "client", "name"],
    "sheikh": ["الشيخ", "sheikh"],
    "phone": ["الجوال", "جوال", "الهاتف", "phone", "mobile"],
    "project": ["رقم المشروع", "المشروع", "اسم المشروع", "project"],
    "notes": ["ملاحظات", "notes"],
    "regions": ["المناطق", "مناطق", "regions"],
    "item": ["اسم الصنف", "الصنف", "نوع السلات", "النوع", "نوع", "item", "type", "basket_type"],
    "qty": ["الكمية", "عدد السلات", "العدد", "qty", "quantity", "count"],
    "unit": ["الوحدة", "unit"],
    "created_at": ["التاريخ والوقت", "التاريخ", "الوقت", "date", "datetime", "created_at"],
}


# ------------------------------------------------------------------
# قاعدة البيانات
# ------------------------------------------------------------------
@contextmanager
def db():
    conn = sqlite3.connect(DB_PATH, timeout=30)  # ينتظر حتى 30 ثانية إذا كان مستخدم آخر يكتب
    conn.execute("PRAGMA busy_timeout=30000")
    conn.execute("PRAGMA foreign_keys=ON")
    try:
        yield conn
        conn.commit()
    finally:
        conn.close()


@st.cache_resource
def init_db():
    with db() as conn:
        conn.execute("PRAGMA journal_mode=WAL")
        cols = [r[1] for r in conn.execute("PRAGMA table_info(vouchers)")]
        old_schema = bool(cols) and "qty" in cols  # النسخة الأولى من التطبيق
        if old_schema:
            conn.execute("DROP INDEX IF EXISTS idx_customer")
            conn.execute("ALTER TABLE vouchers RENAME TO vouchers_old")

        conn.execute(
            """
            CREATE TABLE IF NOT EXISTS vouchers(
                id INTEGER PRIMARY KEY AUTOINCREMENT,
                voucher_no TEXT NOT NULL UNIQUE,
                customer TEXT NOT NULL,
                sheikh TEXT,
                phone TEXT,
                project TEXT,
                notes TEXT,
                created_at TEXT NOT NULL
            )
            """
        )
        conn.execute(
            """
            CREATE TABLE IF NOT EXISTS items(
                id INTEGER PRIMARY KEY AUTOINCREMENT,
                voucher_id INTEGER NOT NULL REFERENCES vouchers(id) ON DELETE CASCADE,
                name TEXT NOT NULL,
                qty REAL NOT NULL,
                unit TEXT
            )
            """
        )
        conn.execute("CREATE INDEX IF NOT EXISTS idx_customer ON vouchers(customer)")
        conn.execute("CREATE INDEX IF NOT EXISTS idx_items_voucher ON items(voucher_id)")
        conn.execute("CREATE TABLE IF NOT EXISTS settings(key TEXT PRIMARY KEY, value TEXT)")
        conn.execute(
            "CREATE TABLE IF NOT EXISTS users(username TEXT PRIMARY KEY, pw_hash TEXT NOT NULL, "
            "role TEXT NOT NULL, created_at TEXT)"
        )
        conn.execute(
            "CREATE TABLE IF NOT EXISTS sessions(token TEXT PRIMARY KEY, username TEXT NOT NULL, "
            "expires TEXT NOT NULL)"
        )
        if "created_by" not in [r[1] for r in conn.execute("PRAGMA table_info(vouchers)")]:
            conn.execute("ALTER TABLE vouchers ADD COLUMN created_by TEXT")
        vcols = [r[1] for r in conn.execute("PRAGMA table_info(vouchers)")]
        if "updated_by" not in vcols:
            conn.execute("ALTER TABLE vouchers ADD COLUMN updated_by TEXT")
        if "updated_at" not in vcols:
            conn.execute("ALTER TABLE vouchers ADD COLUMN updated_at TEXT")
        if "regions" not in vcols:
            conn.execute("ALTER TABLE vouchers ADD COLUMN regions TEXT")

        if old_schema:  # نقل بيانات النسخة الأولى (يبقى الجدول القديم كنسخة احتياطية)
            conn.execute(
                "INSERT INTO vouchers(voucher_no, customer, project, created_at) "
                "SELECT voucher_no, customer, project, created_at FROM vouchers_old"
            )
            conn.execute(
                "INSERT INTO items(voucher_id, name, qty, unit) "
                "SELECT v.id, COALESCE(NULLIF(o.basket_type, ''), ?), o.qty, ? "
                "FROM vouchers_old o JOIN vouchers v ON v.voucher_no = o.voucher_no",
                (DEFAULT_ITEM, DEFAULT_UNIT),
            )
    return True


# ------------------------------------------------------------------
# المستخدمون وكلمات المرور
# ------------------------------------------------------------------
ROLE_AR = {"admin": "مدير", "user": "مستخدم"}
SESSION_HOURS = 12
MIN_PW = 6


def hash_pw(pw: str, salt: bytes = None) -> str:
    salt = salt or secrets.token_bytes(16)
    h = hashlib.pbkdf2_hmac("sha256", pw.encode("utf-8"), salt, 200_000)
    return f"pbkdf2${salt.hex()}${h.hex()}"


def verify_pw(pw: str, stored: str) -> bool:
    try:
        _, salt, h = stored.split("$")
        cand = hashlib.pbkdf2_hmac("sha256", pw.encode("utf-8"), bytes.fromhex(salt), 200_000)
    except ValueError:
        return False
    return hmac.compare_digest(cand.hex(), h)


def count_users() -> int:
    with db() as conn:
        return conn.execute("SELECT COUNT(*) FROM users").fetchone()[0]


def create_user(username: str, pw: str, role: str):
    with db() as conn:
        conn.execute(
            "INSERT INTO users(username, pw_hash, role, created_at) VALUES (?,?,?,?)",
            (username, hash_pw(pw), role, datetime.now().strftime(DATE_FMT)),
        )


def list_users() -> list:
    with db() as conn:
        return conn.execute("SELECT username, role FROM users ORDER BY role, username").fetchall()


def set_password(username: str, pw: str):
    with db() as conn:
        conn.execute("UPDATE users SET pw_hash=? WHERE username=?", (hash_pw(pw), username))
        conn.execute("DELETE FROM sessions WHERE username=?", (username,))


def delete_user(username: str):
    with db() as conn:
        conn.execute("DELETE FROM sessions WHERE username=?", (username,))
        conn.execute("DELETE FROM users WHERE username=?", (username,))


def check_login(username: str, pw: str):
    with db() as conn:
        row = conn.execute(
            "SELECT username, pw_hash, role FROM users WHERE username=?", (username,)
        ).fetchone()
    if row and verify_pw(pw, row[1]):
        return row[0], row[2]
    return None


def create_session(username: str) -> str:
    tok = secrets.token_urlsafe(32)
    now = datetime.now()
    with db() as conn:
        conn.execute("DELETE FROM sessions WHERE expires < ?", (now.strftime(DATE_FMT),))
        conn.execute(
            "INSERT INTO sessions(token, username, expires) VALUES (?,?,?)",
            (tok, username, (now + timedelta(hours=SESSION_HOURS)).strftime(DATE_FMT)),
        )
    return tok


def session_user(tok: str):
    with db() as conn:
        row = conn.execute(
            "SELECT u.username, u.role, s.expires FROM sessions s "
            "JOIN users u ON u.username = s.username WHERE s.token = ?", (tok,)
        ).fetchone()
    if not row or row[2] < datetime.now().strftime(DATE_FMT):
        return None
    return row[0], row[1]


def end_session(tok: str):
    with db() as conn:
        conn.execute("DELETE FROM sessions WHERE token=?", (tok,))


PRINT_DEFAULTS = {"top_mm": 28.4, "bottom_mm": 40.0}


def get_print_settings() -> dict:
    with db() as conn:
        rows = dict(conn.execute("SELECT key, value FROM settings").fetchall())
    out = {}
    for k, default in PRINT_DEFAULTS.items():
        try:
            out[k] = float(rows.get(k, default))
        except ValueError:
            out[k] = default
    return out


def set_print_settings(top_mm: float, bottom_mm: float):
    with db() as conn:
        conn.executemany(
            "INSERT OR REPLACE INTO settings(key, value) VALUES (?, ?)",
            [("top_mm", str(top_mm)), ("bottom_mm", str(bottom_mm))],
        )


TEXT_DEFAULTS = {
    "customer": "السادة /", "sheikh": "الشيخ/", "phone": "جوال", "project": "رقم المشروع",
    "regions": "المناطق",
    "date": "التاريخ", "no": "رقم",
    "intro": DOC_INTRO, "title": DOC_TITLE, "company": COMPANY_NAME,
    "receiver": "المستلم/", "sign": "التوقيع/", "thanks": DOC_THANKS,
    "col_m": "م", "col_item": ITEM_COL, "col_qty": QTY_COL, "col_unit": UNIT_COL,
    "default_item": DEFAULT_ITEM, "default_unit": DEFAULT_UNIT,
}


def get_texts() -> dict:
    with db() as conn:
        rows = dict(conn.execute("SELECT key, value FROM settings WHERE key LIKE 't\\_%' ESCAPE '\\'").fetchall())
    return {k: rows.get("t_" + k, d) for k, d in TEXT_DEFAULTS.items()}


def set_texts(values: dict):
    with db() as conn:
        conn.executemany(
            "INSERT OR REPLACE INTO settings(key, value) VALUES (?, ?)",
            [("t_" + k, (v or "").strip()) for k, v in values.items() if k in TEXT_DEFAULTS],
        )


def reset_texts():
    with db() as conn:
        conn.execute("DELETE FROM settings WHERE key LIKE 't\\_%' ESCAPE '\\'")


def lbl(T: dict, key: str) -> str:
    """اسم الحقل في النماذج: بدون الشرطة المائلة والنقطتين."""
    return T[key].strip().strip("/:").strip() or TEXT_DEFAULTS[key].strip("/: ")


def next_voucher_no() -> int:
    with db() as conn:
        row = conn.execute(
            "SELECT MAX(CAST(voucher_no AS INTEGER)) FROM vouchers"
        ).fetchone()
    return (row[0] or 0) + 1


def distinct_values(table: str, col: str) -> list:
    assert (table, col) in {("vouchers", "customer"), ("vouchers", "project")}
    with db() as conn:
        rows = conn.execute(
            f"SELECT DISTINCT {col} FROM {table} "
            f"WHERE {col} IS NOT NULL AND {col} <> '' ORDER BY {col}"
        ).fetchall()
    return [r[0] for r in rows]


def existing_numbers() -> set:
    with db() as conn:
        return {r[0] for r in conn.execute("SELECT voucher_no FROM vouchers")}


def save_voucher_db(no, customer, sheikh, phone, project, notes, items,
                    created_at=None, created_by=None, regions=""):
    """يحفظ السند ويرجع رقمه. إذا كان no فارغاً يُعطى الرقم التالي داخل نفس المعاملة
    المقفولة للكتابة، فلا يمكن أن يأخذ مستخدمان نفس الرقم."""
    with db() as conn:
        conn.execute("BEGIN IMMEDIATE")  # قفل الكتابة قبل حساب الرقم
        if not no:
            row = conn.execute("SELECT MAX(CAST(voucher_no AS INTEGER)) FROM vouchers").fetchone()
            no = str((row[0] or 0) + 1)
        cur = conn.execute(
            "INSERT INTO vouchers(voucher_no, customer, sheikh, phone, project, notes, "
            "created_at, created_by, regions) VALUES (?,?,?,?,?,?,?,?,?)",
            (no, customer, sheikh, phone, project, notes,
             created_at or datetime.now().strftime(DATE_FMT), created_by, regions or ""),
        )
        vid = cur.lastrowid
        conn.executemany(
            "INSERT INTO items(voucher_id, name, qty, unit) VALUES (?,?,?,?)",
            [(vid, n, q, u) for n, q, u in items],
        )
    return no


def update_voucher_db(vid, no, customer, sheikh, phone, project, notes, items, created_at,
                      updated_by=None, regions=""):
    with db() as conn:
        conn.execute(
            "UPDATE vouchers SET voucher_no=?, customer=?, sheikh=?, phone=?, project=?, "
            "notes=?, regions=?, created_at=?, updated_by=?, updated_at=? WHERE id=?",
            (no, customer, sheikh, phone, project, notes, regions or "", created_at, updated_by,
             datetime.now().strftime(DATE_FMT), vid),
        )
        conn.execute("DELETE FROM items WHERE voucher_id = ?", (vid,))
        conn.executemany(
            "INSERT INTO items(voucher_id, name, qty, unit) VALUES (?,?,?,?)",
            [(vid, n, q, u) for n, q, u in items],
        )


def get_voucher(no: str):
    with db() as conn:
        conn.row_factory = sqlite3.Row
        v = conn.execute("SELECT * FROM vouchers WHERE voucher_no = ?", (no,)).fetchone()
        if not v:
            return None
        its = conn.execute(
            "SELECT name, qty, unit FROM items WHERE voucher_id = ? ORDER BY id", (v["id"],)
        ).fetchall()
    return dict(v), [dict(i) for i in its]


def delete_voucher(no: str) -> int:
    with db() as conn:
        row = conn.execute("SELECT id FROM vouchers WHERE voucher_no = ?", (no,)).fetchone()
        if not row:
            return 0
        conn.execute("DELETE FROM items WHERE voucher_id = ?", (row[0],))
        conn.execute("DELETE FROM vouchers WHERE id = ?", (row[0],))
        return 1


ITEMS_SUMMARY_SQL = (
    "GROUP_CONCAT(i.name || ' (' || "
    "(CASE WHEN i.qty = CAST(i.qty AS INTEGER) THEN CAST(i.qty AS INTEGER) ELSE i.qty END)"
    " || ' ' || COALESCE(i.unit, '') || ')', ' ، ')"
)


def query_vouchers(search: str, limit: int, offset: int):
    where, params = "", []
    if search:
        where = ("WHERE v.voucher_no LIKE ? OR v.customer LIKE ? "
                 "OR v.project LIKE ? OR v.phone LIKE ?")
        params = [f"%{search}%"] * 4
    with db() as conn:
        total = conn.execute(
            f"SELECT COUNT(*) FROM vouchers v {where}", params
        ).fetchone()[0]
        df = pd.read_sql_query(
            f"SELECT v.voucher_no, v.customer, v.project, v.phone, "
            f"SUM(i.qty) AS total, {ITEMS_SUMMARY_SQL} AS items_summary, v.created_at, "
            f"v.created_by, v.updated_by, v.updated_at, v.regions "
            f"FROM vouchers v LEFT JOIN items i ON i.voucher_id = v.id "
            f"{where} GROUP BY v.id ORDER BY v.id DESC LIMIT ? OFFSET ?",
            conn,
            params=params + [limit, offset],
        )
    return df, total


W_ITEM, W_QTY, W_UNIT = "الصنف", "الكمية", "الوحدة"          # عناوين مجموعات الأصناف (الصنف 1، الكمية 1...)
WIDE_FIXED = ["voucher_no", "customer", "sheikh", "phone", "project", "notes", "regions", "created_at"]
EXTRA_COLS = ["المدخل", "آخر تعديل بواسطة", "وقت آخر تعديل"]


def wide_item_headers(n: int) -> list:
    out = []
    for i in range(1, n + 1):
        out += [f"{W_ITEM} {i}", f"{W_QTY} {i}", f"{W_UNIT} {i}"]
    return out


def _num(q):
    try:
        f = float(q)
        return int(f) if f.is_integer() else f
    except (TypeError, ValueError):
        return q


def export_df() -> pd.DataFrame:
    """سند في سطر واحد، وأعمدة الأصناف في الآخر (صنف 1، كمية 1، وحدة 1، صنف 2 ...).
    عدد مجموعات الأصناف = أكبر عدد أصناف في أي سند (بلا حد أعلى). يمكن إعادة استيراده كما هو."""
    with db() as conn:
        vs = conn.execute(
            "SELECT id, voucher_no, customer, sheikh, phone, project, notes, regions, created_at, "
            "created_by, updated_by, updated_at FROM vouchers ORDER BY id"
        ).fetchall()
        its = conn.execute(
            "SELECT voucher_id, name, qty, unit FROM items ORDER BY voucher_id, id"
        ).fetchall()
    by = {}
    for vid, n, q, u in its:
        by.setdefault(vid, []).append((n, _num(q), u or ""))
    maxn = max([len(x) for x in by.values()] or [1])
    head = [LABELS[k] for k in WIDE_FIXED] + EXTRA_COLS + wide_item_headers(maxn)
    rows = []
    for v in vs:
        row = [x if x is not None else "" for x in v[1:]]
        for n, q, u in by.get(v[0], []):
            row += [n, q, u]
        rows.append(row + [""] * (len(head) - len(row)))
    return pd.DataFrame(rows, columns=head)


def wide_template_bytes(groups: int = 5) -> bytes:
    from openpyxl import Workbook
    from openpyxl.styles import Alignment, Font, PatternFill
    wb = Workbook()
    ws = wb.active
    ws.title = "السندات"
    ws.sheet_view.rightToLeft = True
    fixed = [LABELS[k] for k in WIDE_FIXED]
    ws.append(fixed + wide_item_headers(groups))
    for c in ws[1]:
        c.font = Font(bold=True, color="FFFFFF")
        c.alignment = Alignment(horizontal="center", vertical="center")
        c.fill = PatternFill("solid", fgColor="1F3A8A" if c.column <= len(fixed) else "B45309")
        ws.column_dimensions[c.column_letter].width = 24 if c.column == 2 else 16
    ws.freeze_panes = "B2"
    info = wb.create_sheet("تعليمات")
    info.sheet_view.rightToLeft = True
    for line in [
        "كل سند في سطر واحد. اكتب بيانات السند في الأعمدة الزرقاء، وأصنافه في الأعمدة البرتقالية.",
        "كل صنف له ثلاثة أعمدة: الصنف 1 والكمية 1 والوحدة 1، ثم الصنف 2 والكمية 2 والوحدة 2 وهكذا.",
        "لإضافة أصناف أكثر: أضف أعمدة جديدة في آخر الشيت بنفس الأسماء بالأرقام التالية (الصنف 6، الكمية 6، الوحدة 6 ...). لا يوجد حد أقصى.",
        "الصنف الفارغ مع كمية يأخذ الصنف الافتراضي، والوحدة الفارغة تأخذ الوحدة الافتراضية، والمجموعة الفارغة تُتجاهل.",
        "رقم السند الفارغ يُعطى رقماً تلقائياً، والتاريخ الفارغ يأخذ وقت الاستيراد (صيغة التاريخ: 2026-11-10).",
        "عمود «المناطق» اختياري: يُحفظ مع السند ولا يظهر في الطباعة.",
        "اسم العميل مطلوب. السندات الموجودة مسبقاً بنفس الرقم تُتجاهل.",
        "اترك الصف الأول (العناوين) كما هو، ولا تغيّر ترتيب الأعمدة الزرقاء.",
    ]:
        info.append([line])
    info.column_dimensions["A"].width = 120
    out = io.BytesIO()
    wb.save(out)
    return out.getvalue()


# ------------------------------------------------------------------
# أدوات مساعدة
# ------------------------------------------------------------------
def txt(x) -> str:
    if x is None:
        return ""
    try:
        if pd.isna(x):
            return ""
    except (TypeError, ValueError):
        pass
    s = str(x).strip()
    return "" if s.lower() in ("nan", "none", "nat") else s


def clean_no(x) -> str:
    s = txt(x)
    if s.endswith(".0") and s[:-2].isdigit():
        s = s[:-2]
    return s


def fmt_qty(q) -> str:
    try:
        return f"{float(q):g}"
    except (TypeError, ValueError):
        return txt(q)


def to_excel_bytes(df: pd.DataFrame) -> bytes:
    out = io.BytesIO()
    with pd.ExcelWriter(out, engine="openpyxl") as writer:
        df.to_excel(writer, index=False, sheet_name="السندات")
        writer.sheets["السندات"].sheet_view.rightToLeft = True
    return out.getvalue()


def guess_column(key: str, columns: list):
    lowered = {str(c).strip().lower(): c for c in columns}
    for alias in ALIASES[key]:
        if alias.lower() in lowered:
            return lowered[alias.lower()]
    return None


def clean_items(df: pd.DataFrame):
    """يرجع (قائمة الأصناف, رسالة خطأ)."""
    items = []
    for _, r in df.iterrows():
        name = txt(r.get(ITEM_COL))
        qty = pd.to_numeric(r.get(QTY_COL), errors="coerce")
        unit = txt(r.get(UNIT_COL))
        if not name and pd.isna(qty):
            continue  # صف فارغ
        if not name:
            return None, "يوجد صف بدون اسم صنف."
        if pd.isna(qty) or qty <= 0:
            return None, f"الكمية غير صحيحة للصنف: {name}"
        items.append((name, float(qty), unit))
    if not items:
        return None, "أضف صنفاً واحداً على الأقل."
    return items, None


# ------------------------------------------------------------------
# شكل السند للطباعة (A4)
# ------------------------------------------------------------------
VOUCHER_CSS = """
@page { size: A4; margin: 0; }
* { box-sizing: border-box; }
html, body { margin: 0; padding: 0; }
body { font-family: Tahoma, "Segoe UI", Arial, sans-serif; color: #0b0bbf;
       background: #e9e9e9; -webkit-print-color-adjust: exact; print-color-adjust: exact; }
.toolbar { text-align: center; padding: 10px; }
.toolbar button { font-family: inherit; font-size: 14px; padding: 8px 22px; cursor: pointer;
                  border: 1px solid #888; border-radius: 6px; background: #fff; }
.page { position: relative; width: 210mm; height: 296mm; margin: 0 auto 16px;
        background: #fff; box-shadow: 0 0 6px rgba(0,0,0,.3); overflow: hidden; }
.frame { position: absolute; min-height: 643pt; border: 1px solid #000; }
.hdr { position: relative; height: 371.5pt; }
.hdr > div { position: absolute; height: 23pt; line-height: 23pt; font-size: 13pt;
             white-space: nowrap; }
.b { font-weight: bold; }
.c { transform: translateX(-50%); }
.notes { white-space: normal !important; height: auto !important; max-height: 24pt;
         line-height: 12pt !important; font-size: 11pt !important; overflow: hidden;
         max-width: 90%; }
table { width: 100%; border-collapse: collapse; table-layout: fixed; }
td, th { height: 25pt; padding: 0 4pt; text-align: center; font-size: 13pt;
         border-top: 1px solid #000; border-bottom: 1px solid #000; overflow: hidden; }
th { font-weight: bold; }
td + td, th + th { border-inline-start: 1px solid #000; }
td.u { font-weight: bold; font-size: 14pt; }
.ft { height: 24pt; line-height: 24pt; font-weight: bold; font-size: 14pt; }
@media print {
  body { background: #fff; }
  .toolbar { display: none; }
  .page { margin: 0; box-shadow: none; }
  .page + .page { break-before: page; page-break-before: always; }
}
"""


def voucher_page(v: dict, items: list, cfg: dict, T: dict) -> str:
    """صفحة A4 واحدة (div) لسند واحد."""
    e = lambda x: htmllib.escape(txt(x))
    top, bottom = cfg["top_mm"], cfg["bottom_mm"]
    date = txt(v.get("created_at"))[:10]
    gap = 0.4 if len(items) > 3 else 1.0  # تضييق الفراغات إذا كثرت الأصناف

    # إذا لم يتسع السند بين الفراغ العلوي والسفلي يُصغَّر تلقائياً ليبقى الفراغان كما ضُبطا
    est_pt = max(643.0, 371.5 + 24.5 + 26 * len(items) + 100 + 98 * gap + 2 + 20)
    avail_pt = (296 - top - bottom) * 72 / 25.4
    z = max(0.4, min(1.0, avail_pt / est_pt)) if avail_pt > 0 else 1.0
    frame_style = f"top:{top / z:.2f}mm;left:{2 / z:.2f}mm;right:{2 / z:.2f}mm;zoom:{z:.4f}"

    rows = "".join(
        f"<tr><td>{i}</td><td>{e(it['name'])}</td><td>{fmt_qty(it['qty'])}</td>"
        f"<td class='u'>{e(it['unit'])}</td></tr>"
        for i, it in enumerate(items, 1)
    )
    notes = txt(v.get("notes"))
    notes_html = (
        f"<div class='notes' style='right:4.6%;top:100pt'>{e(notes)}</div>" if notes else ""
    )
    return f"""<div class="page"><div class="frame" style="{frame_style}">
  <div class="hdr">
    <div class="b" style="right:4.6%;top:74.7pt">{e(T['project'])}</div>
    <div class="b c" style="left:66.5%;top:74.7pt">{e(v.get('project'))}</div>
    <div class="b c" style="left:41.8%;top:74.7pt">{e(T['date'])}</div>
    <div class="b c" style="left:18%;top:74.7pt">{e(date)}</div>
    {notes_html}
    <div class="b c" style="left:41.8%;top:124pt">{e(T['no'])}</div>
    <div class="b c" style="left:18%;top:124pt">{e(v.get('voucher_no'))}</div>
    <div class="b" style="right:0.4%;top:199.6pt">{e(T['customer'])}</div>
    <div class="c" style="left:44%;top:199.6pt">{e(v.get('customer'))}</div>
    <div class="b" style="right:0.4%;top:224.4pt">{e(T['sheikh'])}</div>
    <div class="c" style="left:44%;top:224.4pt">{e(v.get('sheikh'))}</div>
    <div class="b" style="right:0.4%;top:249.1pt">{e(T['phone'])}</div>
    <div class="c" style="left:44%;top:249.1pt">{e(v.get('phone'))}</div>
    <div class="b c" style="left:50%;top:272.5pt">{e(T['intro'])}</div>
    <div class="b c" style="left:50%;top:347.8pt">{e(T['title'])}</div>
  </div>
  <table>
    <colgroup><col style="width:12%"><col style="width:33.8%"><col style="width:18%"><col style="width:36.2%"></colgroup>
    <thead><tr><th>{e(T['col_m'])}</th><th>{e(T['col_item'])}</th><th>{e(T['col_qty'])}</th><th>{e(T['col_unit'])}</th></tr></thead>
    <tbody>{rows}</tbody>
  </table>
  <div class="ft" style="margin-top:3pt;padding-left:9.5%;text-align:left;font-size:15pt">{e(T['company'])}</div>
  <div class="ft" style="margin-top:{22*gap:.0f}pt;padding-right:0.4%">{e(T['receiver'])}</div>
  <div class="ft" style="margin-top:{26*gap:.0f}pt;padding-right:0.4%">{e(T['sign'])}</div>
  <div class="ft" style="margin-top:{50*gap:.0f}pt;margin-left:17%;margin-bottom:25pt;text-align:center">{e(T['thanks'])}</div>
</div></div>"""


def wrap_doc(pages: str, title: str, button: str = "🖨️ طباعة (A4)", auto_print: bool = False) -> str:
    auto = "<script>window.onload=function(){window.print();}</script>" if auto_print else ""
    return f"""<!doctype html>
<html dir="rtl" lang="ar"><head><meta charset="utf-8">
<title>{htmllib.escape(title)}</title><style>{VOUCHER_CSS}</style></head>
<body>
<div class="toolbar"><button onclick="window.print()">{htmllib.escape(button)}</button></div>
{pages}{auto}
</body></html>"""


def voucher_html(v: dict, items: list, auto_print: bool = False,
                 top_mm=None, bottom_mm=None) -> str:
    cfg = get_print_settings()
    if top_mm is not None:
        cfg["top_mm"] = top_mm
    if bottom_mm is not None:
        cfg["bottom_mm"] = bottom_mm
    page = voucher_page(v, items, cfg, get_texts())
    return wrap_doc(page, f"سند {txt(v.get('voucher_no'))}", auto_print=auto_print)


def batch_html(pairs: list, auto_print: bool = False) -> str:
    """عدة سندات في ملف واحد، كل سند على ورقة A4 مستقلة."""
    cfg, T = get_print_settings(), get_texts()
    pages = "\n".join(voucher_page(v, items, cfg, T) for v, items in pairs)
    return wrap_doc(pages, f"طباعة {len(pairs)} سند", f"🖨️ طباعة {len(pairs)} سند (A4)", auto_print)


def find_browser():
    """يبحث عن Edge أو Chrome لتحويل HTML إلى PDF بدون أي مكتبات إضافية."""
    env = os.environ.get("PDF_BROWSER")
    if env and Path(env).exists():
        return env
    cands = []
    for var in ("ProgramFiles", "ProgramFiles(x86)", "LocalAppData"):
        base = os.environ.get(var)
        if base:
            cands += [Path(base) / "Microsoft/Edge/Application/msedge.exe",
                      Path(base) / "Google/Chrome/Application/chrome.exe"]
    for c in cands:
        if c.exists():
            return str(c)
    for name in ("msedge", "chrome", "google-chrome", "chromium", "chromium-browser"):
        w = shutil.which(name)
        if w:
            return w
    return None


PDF_WORKER = r"""
import sys
from pathlib import Path
from playwright.sync_api import sync_playwright
src, out = sys.argv[1], sys.argv[2]
with sync_playwright() as p:
    browser, last = None, None
    for kw in ({}, {"channel": "msedge"}, {"channel": "chrome"}):
        try:
            browser = p.chromium.launch(**kw)
            break
        except Exception as e:
            last = e
    if browser is None:
        raise SystemExit("no browser: %s" % last)
    page = browser.new_page()
    page.goto(Path(src).as_uri())
    page.pdf(path=out, prefer_css_page_size=True, print_background=True)
    browser.close()
"""


def _pdf_via_playwright(src: Path, out: Path):
    import importlib.util
    if importlib.util.find_spec("playwright") is None:
        return False
    env = dict(os.environ)
    env.setdefault("PLAYWRIGHT_BROWSERS_PATH", "0")  # المتصفح المرفق داخل مجلد البرنامج
    try:  # عملية منفصلة: لا تتأثر بحلقة asyncio الخاصة بـ Streamlit
        subprocess.run([sys.executable, "-c", PDF_WORKER, str(src), str(out)],
                       capture_output=True, timeout=240, env=env)
    except Exception:  # noqa: BLE001
        return False
    return out.exists() and out.stat().st_size > 0


def _pdf_via_cli(src: Path, out: Path, td: Path):
    exe = find_browser()
    if not exe:
        return False
    cmd = [exe, "--headless", "--disable-gpu", "--no-sandbox", "--hide-scrollbars",
           "--no-pdf-header-footer", "--print-to-pdf-no-header", "--disable-background-networking",
           "--no-first-run", f"--user-data-dir={td / 'profile'}", f"--print-to-pdf={out}", src.as_uri()]
    try:
        subprocess.run(cmd, capture_output=True, timeout=90)
    except Exception:  # noqa: BLE001
        return False
    return out.exists() and out.stat().st_size > 0


def html_to_pdf(html: str):
    """يرجع (bytes, None) عند النجاح أو (None, سبب الفشل). يجرّب Playwright ثم Edge/Chrome مباشرة."""
    with tempfile.TemporaryDirectory() as td:
        td = Path(td)
        src, out = td / "in.html", td / "out.pdf"
        src.write_text(html, encoding="utf-8")
        if _pdf_via_playwright(src, out) or _pdf_via_cli(src, out, td):
            return out.read_bytes(), None
    return None, "تعذّر إنشاء PDF تلقائياً على السيرفر (لا يوجد متصفح مناسب للتحويل)."


def show_voucher(no: str, key: str):
    got = get_voucher(no)
    if not got:
        st.warning("لم يتم العثور على السند.")
        return
    v, items = got
    page = voucher_html(v, items)
    if hasattr(st, "iframe"):  # النسخ الحديثة من Streamlit
        st.iframe(page, height=1250)
    else:
        components.html(page, height=1250, scrolling=True)
    safe = re.sub(r"[^\w\-]", "_", no)
    st.download_button(
        "⬇️ تحميل ملف السند (يفتح في المتصفح ويطبع مباشرة)",
        voucher_html(v, items, auto_print=True).encode("utf-8"),
        f"voucher_{safe}.html",
        "text/html",
        key=f"dl_{key}_{safe}",
    )


# ------------------------------------------------------------------
# تبويب إدخال سند
# ------------------------------------------------------------------
def items_column_config(T: dict) -> dict:
    return {
        ITEM_COL: st.column_config.TextColumn(T["col_item"] or ITEM_COL, required=True),
        QTY_COL: st.column_config.NumberColumn(T["col_qty"] or QTY_COL, min_value=0, format="%g"),
        UNIT_COL: st.column_config.TextColumn(T["col_unit"] or UNIT_COL),
    }


def choose_or_new(label, options, key, ver, default=""):
    choices = [NEW_OPTION] + options
    idx = choices.index(default) if default and default in options else 0
    sel = st.selectbox(label, choices, index=idx, key=f"{key}_sel_{ver}")
    if sel == NEW_OPTION:
        initial = default if default and default not in options else ""
        return st.text_input(f"{label} (اكتب الاسم الجديد)", value=initial,
                             key=f"{key}_new_{ver}").strip()
    return sel


def entry_tab():
    ss = st.session_state
    T = get_texts()
    ver = ss.setdefault("ver", 0)
    d = ss.get("defaults", {})

    flash = ss.pop("flash", None)
    if flash:
        (st.success if flash[0] == "success" else st.error)(flash[1])

    created = None
    c1, c2 = st.columns(2)
    with c1:
        st.radio("رقم السند", ["تلقائي", "يدوي"], horizontal=True, key="no_mode")
        if ss["no_mode"] == "تلقائي":
            st.info(f"رقم السند التالي: **{next_voucher_no()}**")
            manual_no = ""
        else:
            manual_no = st.text_input("أدخل رقم السند", key=f"no_manual_{ver}").strip()
        customer = choose_or_new(f"{lbl(T, 'customer')} (اسم العميل)", distinct_values("vouchers", "customer"),
                                 "customer", ver, d.get("customer", ""))
        sheikh = st.text_input(f"{lbl(T, 'sheikh')} (اختياري)", value=d.get("sheikh", ""),
                               key=f"sheikh_{ver}").strip()
        phone = st.text_input(f"{lbl(T, 'phone')} (اختياري)", value=d.get("phone", ""),
                              key=f"phone_{ver}").strip()
    with c2:
        project = choose_or_new(lbl(T, "project"), distinct_values("vouchers", "project"),
                                "project", ver, d.get("project", ""))
        notes = st.text_input("ملاحظات (اختياري - تظهر تحت المشروع في السند)",
                              max_chars=120, key=f"notes_{ver}").strip()
        regions = st.text_area(f"{lbl(T, 'regions')} (اختياري - لا تظهر في الطباعة)",
                               height=100, key=f"regions_{ver}").strip()
        st.checkbox("الاحتفاظ بالعميل والمشروع والأصناف بعد الحفظ", value=True, key="keep")
        st.radio("التاريخ والوقت", ["تلقائي (لحظة الحفظ)", "يدوي"], horizontal=True, key="date_mode")
        if ss["date_mode"] == "يدوي":
            dc, tc = st.columns(2)
            now_ = datetime.now()
            d_in = dc.date_input("التاريخ", value=now_.date(), key=f"date_{ver}")
            t_in = tc.time_input("الوقت", value=now_.time().replace(second=0, microsecond=0),
                                 key=f"time_{ver}")
            created = datetime.combine(d_in, t_in).strftime(DATE_FMT)

    st.markdown("**جدول الأصناف** (أضف صفاً من آخر الجدول، وعدّل الاسم والكمية والوحدة مباشرة، "
                "ولحذف صف حدده واضغط Delete)")
    base = d.get("items")
    if base is None:
        base = pd.DataFrame([{ITEM_COL: T["default_item"] or DEFAULT_ITEM, QTY_COL: 1,
                              UNIT_COL: T["default_unit"] or DEFAULT_UNIT}])
    edited = st.data_editor(
        base,
        num_rows="dynamic",
        hide_index=True,
        key=f"items_{ver}",
        column_config=items_column_config(T),
    )

    if st.button("💾 حفظ السند", type="primary"):
        items, err = clean_items(edited)
        if not customer:
            err = "اسم العميل (السادة) مطلوب."
        elif ss["no_mode"] == "يدوي" and not manual_no:
            err = "أدخل رقم السند أو اختر الوضع التلقائي."
        if err:
            st.error(err)
        else:
            try:
                no = save_voucher_db(manual_no or None, customer, sheikh, phone, project, notes,
                                     items, created, created_by=ss["user"][0], regions=regions)
            except sqlite3.IntegrityError:
                st.error(f"رقم السند {manual_no} موجود مسبقاً.")
            except sqlite3.OperationalError:
                st.error("قاعدة البيانات مشغولة حالياً، أعد المحاولة بعد لحظات.")
            else:
                ss["last_saved"] = no
                ss["flash"] = ("success", f"تم حفظ السند رقم {no} ✔")
                ss["defaults"] = (
                    {"customer": customer, "sheikh": sheikh, "phone": phone, "project": project,
                     "items": pd.DataFrame(items, columns=[ITEM_COL, QTY_COL, UNIT_COL])}
                    if ss["keep"] else {}
                )
                ss["ver"] = ver + 1
                st.rerun()

    last = ss.get("last_saved")
    if last:
        st.divider()
        st.subheader(f"🖨️ السند رقم {last}")
        show_voucher(last, "last")


# ------------------------------------------------------------------
# تعديل سند محفوظ
# ------------------------------------------------------------------
def edit_form(no: str, key: str, preview: bool = False):
    ss = st.session_state
    got = get_voucher(no)
    if not got:
        st.warning("لم يتم العثور على سند بهذا الرقم.")
        return
    v, items = got
    T = get_texts()
    vid = v["id"]
    ver = ss.setdefault(f"ev_{vid}", 0)
    k = f"{key}_{vid}_{ver}"

    flash = ss.pop(f"edit_flash_{vid}", None)
    if flash:
        st.success(flash)

    try:
        dt = datetime.strptime(v["created_at"], DATE_FMT)
    except (TypeError, ValueError):
        dt = pd.to_datetime(v["created_at"], errors="coerce")
        dt = dt.to_pydatetime() if pd.notna(dt) else datetime.now()

    c1, c2 = st.columns(2)
    with c1:
        new_no = st.text_input("رقم السند", value=v["voucher_no"], key=f"{k}_no").strip()
        customer = choose_or_new(f"{lbl(T, 'customer')} (اسم العميل)", distinct_values("vouchers", "customer"),
                                 f"{k}_cust", 0, v["customer"])
        sheikh = st.text_input(f"{lbl(T, 'sheikh')} (اختياري)", value=v["sheikh"] or "",
                               key=f"{k}_sheikh").strip()
        phone = st.text_input(f"{lbl(T, 'phone')} (اختياري)", value=v["phone"] or "",
                              key=f"{k}_phone").strip()
    with c2:
        project = choose_or_new(lbl(T, "project"), distinct_values("vouchers", "project"),
                                f"{k}_proj", 0, v["project"] or "")
        notes = st.text_input("ملاحظات (اختياري)", value=v["notes"] or "", max_chars=120,
                              key=f"{k}_notes").strip()
        regions = st.text_area(f"{lbl(T, 'regions')} (اختياري - لا تظهر في الطباعة)",
                               value=v.get("regions") or "", height=100, key=f"{k}_regions").strip()
        dc, tc = st.columns(2)
        d_in = dc.date_input("التاريخ", value=dt.date(), key=f"{k}_d")
        t_in = tc.time_input("الوقت", value=dt.time().replace(second=0, microsecond=0), key=f"{k}_t")

    base = pd.DataFrame(
        [{ITEM_COL: i["name"], QTY_COL: i["qty"], UNIT_COL: i["unit"] or ""} for i in items]
    )
    edited = st.data_editor(
        base, num_rows="dynamic", hide_index=True, key=f"{k}_items",
        column_config=items_column_config(T),
    )

    if st.button("💾 حفظ التعديلات", type="primary", key=f"{k}_save"):
        new_items, err = clean_items(edited)
        if not new_no:
            err = "رقم السند مطلوب."
        elif not customer:
            err = "اسم العميل (السادة) مطلوب."
        if err:
            st.error(err)
        else:
            try:
                update_voucher_db(vid, new_no, customer, sheikh, phone, project, notes, new_items,
                                  datetime.combine(d_in, t_in).strftime(DATE_FMT),
                                  updated_by=ss["user"][0], regions=regions)
            except sqlite3.IntegrityError:
                st.error(f"رقم السند {new_no} مستخدم في سند آخر.")
            else:
                ss[f"edit_flash_{vid}"] = f"تم حفظ التعديلات على السند رقم {new_no} ✔"
                ss[f"ev_{vid}"] = ver + 1
                st.rerun()

    if preview:
        st.divider()
        st.subheader(f"🖨️ السند رقم {v['voucher_no']}")
        show_voucher(v["voucher_no"], f"edit_{vid}")


def edit_tab():
    no = st.text_input("رقم السند المراد تعديله", key="edit_lookup").strip()
    st.caption("أو من تبويب «السندات والبحث» اضغط على صف السند ثم افتح «✏️ تعديل هذا السند».")
    if no:
        edit_form(no, "e", preview=True)


# ------------------------------------------------------------------
# الطباعة المجمّعة
# ------------------------------------------------------------------
MAX_BATCH = 300


def search_numbers(search: str, limit: int) -> list:
    where, params = "", []
    if search:
        where = "WHERE voucher_no LIKE ? OR customer LIKE ? OR project LIKE ? OR phone LIKE ?"
        params = [f"%{search}%"] * 4
    with db() as conn:
        rows = conn.execute(
            f"SELECT voucher_no FROM vouchers {where} ORDER BY id DESC LIMIT ?", params + [limit]
        ).fetchall()
    return [r[0] for r in rows]


def range_numbers(first: int, last: int, limit: int) -> list:
    with db() as conn:
        rows = conn.execute(
            "SELECT voucher_no FROM vouchers WHERE voucher_no GLOB '[0-9]*' "
            "AND CAST(voucher_no AS INTEGER) BETWEEN ? AND ? "
            "ORDER BY CAST(voucher_no AS INTEGER) LIMIT ?",
            (first, last, limit),
        ).fetchall()
    return [r[0] for r in rows]


def get_vouchers_full(nos: list) -> list:
    """[(سند, أصنافه)] مرتبة من الأقدم إلى الأحدث."""
    out = {}
    with db() as conn:
        conn.row_factory = sqlite3.Row
        for i in range(0, len(nos), 400):
            chunk = nos[i:i + 400]
            q = ",".join("?" * len(chunk))
            for v in conn.execute(f"SELECT * FROM vouchers WHERE voucher_no IN ({q})", chunk):
                out[v["id"]] = (dict(v), [])
        ids = list(out)
        for i in range(0, len(ids), 400):
            chunk = ids[i:i + 400]
            q = ",".join("?" * len(chunk))
            for r in conn.execute(
                f"SELECT voucher_id, name, qty, unit FROM items WHERE voucher_id IN ({q}) ORDER BY id",
                chunk,
            ):
                out[r["voucher_id"]][1].append({"name": r["name"], "qty": r["qty"], "unit": r["unit"]})
    return [out[k] for k in sorted(out)]


def batch_print_panel(search: str, total_results: int, selected: list):
    ss = st.session_state
    st.caption(f"الحد الأقصى {MAX_BATCH} سند في المرة الواحدة. كل سند يُطبع على ورقة A4 مستقلة.")
    labels = {
        "sel": f"السندات المحددة في الجدول ({len(selected)})",
        "all": f"كل نتائج البحث الحالية ({total_results:,})",
        "range": "من رقم إلى رقم",
    }
    src = st.radio("اختر السندات", list(labels), format_func=labels.get, horizontal=True,
                   key="batch_src")
    nos, note = [], ""
    if src == "sel":
        nos = list(selected)
        if not nos:
            note = "حدد صفوفاً من الجدول أعلاه (اضغط على مربع الصف، ويمكن تحديد أكثر من صف)."
    elif src == "all":
        nos = search_numbers(search, MAX_BATCH + 1)
        if len(nos) > MAX_BATCH:
            nos = nos[:MAX_BATCH]
            note = f"النتائج أكثر من {MAX_BATCH}، سيُؤخذ أحدث {MAX_BATCH} فقط. ضيّق البحث أو استخدم «من رقم إلى رقم»."
    else:
        r1, r2 = st.columns(2)
        a = r1.text_input("من رقم", key="batch_from").strip()
        b = r2.text_input("إلى رقم", key="batch_to").strip()
        if a.isdigit() and b.isdigit():
            nos = range_numbers(int(a), int(b), MAX_BATCH + 1)
            if len(nos) > MAX_BATCH:
                nos = nos[:MAX_BATCH]
                note = f"المدى أكبر من {MAX_BATCH} سند، سيُؤخذ أول {MAX_BATCH} فقط."
        else:
            note = "اكتب رقمَي البداية والنهاية (أرقام فقط)."
    if note:
        st.info(note)
    st.write(f"عدد السندات التي ستُجهَّز: **{len(nos)}**")

    if st.button("🖨️ تجهيز الطباعة", type="primary", key="batch_go", disabled=not nos):
        pairs = get_vouchers_full(nos)
        ss["batch"] = {
            "n": len(pairs),
            "preview": batch_html(pairs),
            "print": batch_html(pairs, auto_print=True),
            "pdf": None,
            "pdf_err": None,
        }
        st.rerun()

    bt = ss.get("batch")
    if not bt:
        return
    st.success(f"تم تجهيز {bt['n']} سند. اختر الطباعة أو تحميل PDF.")
    stamp = datetime.now().strftime("%Y%m%d_%H%M")
    d1, d2 = st.columns(2)
    d1.download_button(
        "⬇️ ملف الطباعة (يفتح في المتصفح ويطبع الكل)", bt["print"].encode("utf-8"),
        f"vouchers_print_{stamp}.html", "text/html", key="batch_dl_html",
    )
    if d2.button("📄 تجهيز PDF", key="batch_pdf_btn"):
        with st.spinner("جارٍ إنشاء ملف PDF..."):
            bt["pdf"], bt["pdf_err"] = html_to_pdf(bt["preview"])
    if bt.get("pdf"):
        st.download_button(
            "⬇️ تحميل PDF", bt["pdf"], f"vouchers_{stamp}.pdf", "application/pdf", key="batch_dl_pdf",
        )
    elif bt.get("pdf_err"):
        st.warning(
            f"{bt['pdf_err']} البديل: حمّل «ملف الطباعة» وافتحه، ثم من نافذة الطباعة اختر "
            "الطابعة «Save as PDF» (أو «Microsoft Print to PDF») واحفظ الملف."
        )
    if bt["n"] <= 10:
        with st.expander("👁️ معاينة", expanded=False):
            if hasattr(st, "iframe"):
                st.iframe(bt["preview"], height=900)
            else:
                components.html(bt["preview"], height=900, scrolling=True)


# ------------------------------------------------------------------
# تبويب السندات والبحث
# ------------------------------------------------------------------
def view_tab(is_admin: bool = False):
    if st_keyup:
        search = st_keyup("🔎 بحث برقم السند أو اسم العميل", key="search", debounce=250)
    else:
        search = st.text_input("🔎 بحث برقم السند أو اسم العميل (اضغط Enter)", key="search")
    search = (search or "").strip()

    p1, p2 = st.columns(2)
    page_size = p1.selectbox("عدد الصفوف في الصفحة", [100, 500, 1000], index=0)
    _, total = query_vouchers(search, 1, 0)
    pages = max(1, -(-total // page_size))
    page = p2.number_input("الصفحة", min_value=1, max_value=pages, value=1, step=1)

    df, total = query_vouchers(search, page_size, (page - 1) * page_size)
    st.caption(f"إجمالي النتائج: {total:,}  |  الصفحة {page} من {pages}  |  "
               "حدد صفاً لعرض السند وتعديله، أو عدة صفوف لطباعتها معاً")
    shown = df.copy()
    shown["total"] = shown["total"].map(lambda x: fmt_qty(x) if pd.notna(x) else "")
    shown = shown.rename(columns={
        "voucher_no": "رقم السند", "customer": "السادة", "project": "المشروع",
        "phone": "الجوال", "total": "إجمالي الكمية", "items_summary": "الأصناف",
        "created_at": "التاريخ والوقت", "created_by": "المدخل",
        "updated_by": "آخر تعديل بواسطة", "updated_at": "وقت آخر تعديل", "regions": "المناطق",
    })
    event = st.dataframe(
        shown, hide_index=True, on_select="rerun", selection_mode="multi-row",
        key=f"tbl_{search}_{page}_{page_size}",
    )
    rows = list(event.selection.rows) if event is not None else []
    picked = [df.iloc[i]["voucher_no"] for i in rows if i < len(df)]

    with st.expander("🖨️ طباعة / PDF لعدة سندات", expanded=len(picked) > 1):
        batch_print_panel(search, total, picked)

    if len(picked) == 1:
        no = picked[0]
        st.subheader(f"🖨️ السند رقم {no}")
        show_voucher(no, "view")
        with st.expander("✏️ تعديل هذا السند"):
            edit_form(no, "v")
    elif len(picked) > 1:
        st.caption(f"تم تحديد {len(picked)} سند. لعرض سند واحد وتعديله حدد صفاً واحداً فقط.")

    if is_admin:
        with st.expander("🗑️ حذف سند"):
            del_no = st.text_input("رقم السند المراد حذفه", key="del_no")
            sure = st.checkbox("أنا متأكد من الحذف", key="del_sure")
            if st.button("حذف"):
                if del_no.strip() and sure:
                    if delete_voucher(del_no.strip()):
                        st.success("تم الحذف.")
                    else:
                        st.warning("لا يوجد سند بهذا الرقم.")
                else:
                    st.warning("أدخل رقم السند وأكّد الحذف.")


# ------------------------------------------------------------------
# تبويب الاستيراد
# ------------------------------------------------------------------
_GROUP_RE = re.compile(r"^(?:ال)?(صنف|كمي[ةه]|وحد[ةه]|item|quantity|qty|unit)[\s_\-\.]*(\d+)$", re.I)
_DIGITS = str.maketrans("٠١٢٣٤٥٦٧٨٩۰۱۲۳۴۵۶۷۸۹", "01234567890123456789")


def detect_item_groups(columns) -> list:
    """يتعرف على أعمدة (الصنف N، الكمية N، الوحدة N) مهما كان عددها. يرجع [(N, عمود الصنف, عمود الكمية, عمود الوحدة)]."""
    found = {}
    for c in columns:
        m = _GROUP_RE.match(str(c).translate(_DIGITS).strip())
        if not m:
            continue
        w = m.group(1).lower()
        kind = "item" if (w.startswith("صنف") or w == "item") else \
               "qty" if (w.startswith("كمي") or w in ("qty", "quantity")) else "unit"
        found.setdefault(int(m.group(2)), {}).setdefault(kind, c)
    return [(n, g.get("item"), g["qty"], g.get("unit")) for n, g in sorted(found.items()) if "qty" in g]


def run_import(src: pd.DataFrame, mapping: dict, by=None, groups=None):
    with db() as conn:
        conn.execute("BEGIN IMMEDIATE")  # لا يدخل أحد سنداً أثناء الاستيراد فلا تتعارض الأرقام
        return _run_import(conn, src, mapping, by, groups or [])


def _run_import(conn, src: pd.DataFrame, mapping: dict, by=None, groups=None):
    """يرجع (سندات مضافة، سندات مكررة، صفوف متجاهلة، أصناف بكمية غير صحيحة)."""
    NONE = "— لا شيء —"
    T = get_texts()
    d_item, d_unit = T["default_item"] or DEFAULT_ITEM, T["default_unit"] or DEFAULT_UNIT
    src = src.fillna("")

    def cell(r, key):
        c = mapping.get(key, NONE)
        return r.get(c, "") if c != NONE else ""

    if mapping.get("created_at", NONE) != NONE:
        parsed_dates = pd.to_datetime(src[mapping["created_at"]], errors="coerce").tolist()
    else:
        parsed_dates = [pd.NaT] * len(src)
    now = datetime.now().strftime(DATE_FMT)

    recs = src.to_dict("records")
    existing = existing_numbers()

    explicit = []
    for r in recs:
        s_ = clean_no(cell(r, "voucher_no"))
        if s_.isdigit():
            explicit.append(int(s_))
    counter = max([next_voucher_no() - 1] + explicit) + 1

    def row_items(r):
        items, bad = [], 0
        if groups:  # سند في سطر واحد: مجموعات (الصنف N، الكمية N، الوحدة N)
            for _, ic, qc, uc in groups:
                name = txt(r.get(ic)) if ic else ""
                qraw = txt(r.get(qc))
                if not name and not qraw:
                    continue
                q = pd.to_numeric(qraw, errors="coerce")
                if pd.isna(q) or q <= 0:
                    bad += 1
                    continue
                items.append((name or d_item, float(q), (txt(r.get(uc)) if uc else "") or d_unit))
        else:       # الصيغة القديمة: صف لكل صنف
            q = pd.to_numeric(txt(cell(r, "qty")), errors="coerce")
            if pd.isna(q) or q <= 0:
                bad += 1
            else:
                items.append((txt(cell(r, "item")) or d_item, float(q), txt(cell(r, "unit")) or d_unit))
        return items, bad

    vouchers, order = {}, []
    skipped_rows = bad_items = 0
    dup = set()
    for idx, r in enumerate(recs):
        if not any(txt(v) for v in r.values()):
            continue  # صف فارغ
        items, bad = row_items(r)
        bad_items += bad
        if not items:
            skipped_rows += 1
            continue
        no = clean_no(cell(r, "voucher_no"))
        if no and no in existing:
            dup.add(no)
            continue
        if no and no in vouchers:
            vouchers[no]["items"].extend(items)
            continue
        customer = txt(cell(r, "customer"))
        if not customer:
            skipped_rows += 1
            continue
        if not no:
            no = str(counter)
            counter += 1
        d = parsed_dates[idx]
        vouchers[no] = {
            "customer": customer,
            "sheikh": txt(cell(r, "sheikh")),
            "phone": txt(cell(r, "phone")),
            "project": txt(cell(r, "project")),
            "notes": txt(cell(r, "notes")),
            "regions": txt(cell(r, "regions")),
            "created_at": d.strftime(DATE_FMT) if pd.notna(d) else now,
            "items": items,
        }
        order.append(no)

    for no in order:
        v = vouchers[no]
        cur = conn.execute(
            "INSERT INTO vouchers(voucher_no, customer, sheikh, phone, project, notes, "
            "created_at, created_by, regions) VALUES (?,?,?,?,?,?,?,?,?)",
            (no, v["customer"], v["sheikh"], v["phone"], v["project"], v["notes"],
             v["created_at"], by, v["regions"]),
        )
        conn.executemany(
            "INSERT INTO items(voucher_id, name, qty, unit) VALUES (?,?,?,?)",
            [(cur.lastrowid, n, q, u) for n, q, u in v["items"]],
        )
    return len(order), len(dup), skipped_rows, bad_items


def import_tab():
    st.write("ارفع ملف Excel (xlsx) أو CSV. **كل سند في سطر واحد**، وأعمدة الأصناف في آخر الشيت "
             "(الصنف 1، الكمية 1، الوحدة 1، ثم الصنف 2، الكمية 2، الوحدة 2 ...) بلا حد أقصى لعدد الأصناف.")
    st.download_button(
        "📄 تحميل قالب فارغ", wide_template_bytes(5), "قالب_السندات.xlsx",
        "application/vnd.openxmlformats-officedocument.spreadsheetml.sheet",
    )

    up = st.file_uploader("اختر الملف", type=["xlsx", "csv"])
    if up is None:
        return
    try:
        if up.name.lower().endswith(".csv"):
            raw = up.getvalue()
            try:
                src = pd.read_csv(io.BytesIO(raw), dtype=str, encoding="utf-8-sig")
            except UnicodeDecodeError:
                src = pd.read_csv(io.BytesIO(raw), dtype=str, encoding="cp1256")
        else:
            src = pd.read_excel(up, dtype=str)
    except Exception as e:
        st.error(f"تعذّرت قراءة الملف: {e}")
        return

    st.write(f"عدد الصفوف في الملف: **{len(src):,}**")
    st.dataframe(src.head(10), hide_index=True)

    groups = detect_item_groups(src.columns)
    st.subheader("ربط الأعمدة")
    if groups:
        st.success(f"✔ تم التعرّف على صيغة «سند في سطر واحد»: {len(groups)} مجموعة أصناف "
                   f"(من الصنف {groups[0][0]} إلى الصنف {groups[-1][0]}).")
        keys = WIDE_FIXED
    else:
        st.info("لم أجد أعمدة بصيغة (الصنف 1، الكمية 1 ...)، فسيُقرأ الملف بالصيغة القديمة: "
                "صف لكل صنف، والصفوف التي تحمل نفس رقم السند تُجمَّع في سند واحد.")
        keys = list(LABELS)

    NONE = "— لا شيء —"
    options = [NONE] + [str(c) for c in src.columns]
    mapping = {}
    cols = st.columns(3)
    for i, key in enumerate(keys):
        g = guess_column(key, list(src.columns))
        idx = options.index(str(g)) if g is not None else 0
        mapping[key] = cols[i % 3].selectbox(LABELS[key], options, index=idx, key=f"map_{key}")

    T = get_texts()
    st.caption(
        f"المطلوب: اسم العميل" + ("" if groups else " وعدد السلات") + ". "
        f"الصنف الفارغ يصبح «{T['default_item']}» والوحدة الفارغة «{T['default_unit']}». "
        "رقم السند الفارغ يُعطى رقماً تلقائياً، والتاريخ الفارغ يأخذ وقت الاستيراد. "
        "السندات الموجودة مسبقاً بنفس الرقم تُتجاهل."
    )

    if st.button("⬆️ بدء الاستيراد", type="primary"):
        if mapping["customer"] == NONE or (not groups and mapping["qty"] == NONE):
            st.error("اربط عمود اسم العميل" + ("" if groups else " وعمود الكمية") + " على الأقل.")
            return
        added, dup, skipped, bad = run_import(src, mapping, st.session_state["user"][0], groups)
        st.success(f"تم استيراد {added:,} سند. مكرر/متجاهَل: {dup:,}. "
                   f"صفوف ناقصة (بدون عميل أو أصناف): {skipped:,}. أصناف بكمية غير صحيحة: {bad:,}.")


# ------------------------------------------------------------------
# تبويب التصدير
# ------------------------------------------------------------------
def export_tab():
    with db() as conn:
        count = conn.execute("SELECT COUNT(*) FROM vouchers").fetchone()[0]
    st.write(f"عدد السندات المخزنة: **{count:,}**")

    if st.button("تجهيز ملفات التصدير"):
        data = export_df()
        stamp = datetime.now().strftime("%Y%m%d_%H%M")
        st.session_state["export"] = (
            stamp, to_excel_bytes(data), data.to_csv(index=False).encode("utf-8-sig"),
        )

    if "export" in st.session_state:
        stamp, xlsx_bytes, csv_bytes = st.session_state["export"]
        e1, e2 = st.columns(2)
        e1.download_button(
            "⬇️ تحميل Excel", xlsx_bytes, f"vouchers_{stamp}.xlsx",
            "application/vnd.openxmlformats-officedocument.spreadsheetml.sheet",
        )
        e2.download_button("⬇️ تحميل CSV", csv_bytes, f"vouchers_{stamp}.csv", "text/csv")

    st.info(f"💡 نسخة احتياطية كاملة: انسخ ملف قاعدة البيانات `{DB_PATH.name}` الموجود بجانب app.py.")


# ------------------------------------------------------------------
# تبويب إعدادات الطباعة
# ------------------------------------------------------------------
TEXT_GROUPS = [
    ("مسميات الحقول", [
        ("customer", "مسمى العميل (السادة)"), ("sheikh", "مسمى الشيخ"), ("phone", "مسمى الجوال"),
        ("project", "مسمى المشروع"), ("date", "مسمى التاريخ"), ("no", "مسمى رقم السند"),
        ("regions", "مسمى المناطق (لا يُطبع)"),
    ]),
    ("عناوين جدول الأصناف", [
        ("col_m", "عمود الترقيم"), ("col_item", "عمود اسم الصنف"),
        ("col_qty", "عمود الكمية"), ("col_unit", "عمود الوحدة"),
    ]),
    ("نصوص السند", [
        ("company", "اسم الشركة"), ("title", "عنوان السند"), ("intro", "عبارة الافتتاح"),
        ("receiver", "مسمى المستلم"), ("sign", "مسمى التوقيع"), ("thanks", "عبارة الشكر"),
    ]),
    ("القيم الافتراضية عند الإدخال", [
        ("default_item", "الصنف الافتراضي"), ("default_unit", "الوحدة الافتراضية"),
    ]),
]


def print_settings_tab():
    st.subheader("📐 فراغات الطباعة")
    cfg = get_print_settings()
    c1, c2 = st.columns(2)
    top = c1.number_input("الفراغ العلوي (مم)", 0.0, 120.0, cfg["top_mm"], 1.0, key="ps_top")
    bottom = c2.number_input("الفراغ السفلي (مم)", 0.0, 120.0, cfg["bottom_mm"], 1.0, key="ps_bottom")
    st.caption("الفراغ العلوي والسفلي يبقيان ثابتين على A4 مهما كان عدد الأصناف؛ "
               "إن كثرت يُصغَّر السند قليلاً ليتسع بينهما. "
               "وعند الطباعة اجعل Margins: None وأوقف Headers and footers في نافذة الطباعة.")
    if st.button("💾 حفظ إعدادات الطباعة", type="primary"):
        set_print_settings(top, bottom)
        st.success("تم الحفظ.")

    st.divider()
    st.subheader("✏️ نصوص السند ومسميات الحقول")
    st.caption("غيّر أي كلمة تظهر في السند أو في شاشات الإدخال (مثل «الشيخ» أو «جوال» أو «رقم المشروع»). "
               "يسري التغيير فوراً على كل السندات القديمة والجديدة عند عرضها أو طباعتها، "
               "ولا يغيّر البيانات المحفوظة. أما تغيير أماكن الحقول أو إضافة حقل جديد فيحتاج تعديلاً في البرنامج.")
    T = get_texts()
    with st.form("texts_form"):
        new = {}
        for title, keys in TEXT_GROUPS:
            st.markdown(f"**{title}**")
            cols = st.columns(3)
            for i, (k, label) in enumerate(keys):
                new[k] = cols[i % 3].text_input(label, value=T[k], key=f"tx_{k}")
        if st.form_submit_button("💾 حفظ النصوص", type="primary"):
            set_texts(new)
            st.success("تم حفظ النصوص.")
    if st.button("↩️ استعادة النصوص الافتراضية"):
        reset_texts()
        for k in list(st.session_state):
            if str(k).startswith("tx_"):
                del st.session_state[k]
        st.rerun()


# ------------------------------------------------------------------
# شعار الشركة (مضمَّن داخل الملف حتى لا يحتاج ملفات إضافية)
# ------------------------------------------------------------------
LOGO_B64 = (
    "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"
)
ICON_B64 = (
    "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"
)

BRAND_CSS = """
<style>
.stApp { direction: rtl; text-align: right; }
.brand { display: flex; align-items: center; gap: 18px; margin: 0 0 .6rem; }
.brand-logo { background: #fff; border-radius: 12px; padding: 6px 16px;
              box-shadow: 0 1px 5px rgba(0,0,0,.22); flex: none; }
.brand-logo img { height: 62px; width: auto; display: block; }
.brand-title { font-size: 1.9rem; font-weight: 700; line-height: 1.25; }
.brand.center { flex-direction: column; justify-content: center; text-align: center; gap: 10px; margin-bottom: 1rem; }
.brand.center .brand-logo img { height: 92px; }
@media (max-width: 640px) {
  .brand-logo img { height: 44px; }
  .brand-title { font-size: 1.3rem; }
}
</style>
"""


def page_icon():
    """أيقونة تبويب المتصفح (تُستخدم أيضاً عند إنشاء اختصار من Chrome)."""
    try:
        from PIL import Image
        return Image.open(io.BytesIO(base64.b64decode(ICON_B64)))
    except Exception:  # noqa: BLE001
        return "🧺"


def brand_header(title: str, center: bool = False):
    """الشعار + عنوان البرنامج، يظهر أعلى كل صفحة."""
    st.markdown(
        f'<div class="brand{" center" if center else ""}">'
        f'<div class="brand-logo"><img alt="AFAQ" src="data:image/png;base64,{LOGO_B64}"></div>'
        f'<div class="brand-title">{htmllib.escape(title)}</div></div>',
        unsafe_allow_html=True,
    )


# ------------------------------------------------------------------
# تسجيل الدخول والحساب
# ------------------------------------------------------------------
def current_user():
    ss = st.session_state
    if "user" in ss:
        return ss["user"]
    tok = st.query_params.get("t")
    if tok:
        u = session_user(tok)
        if u:
            ss["user"], ss["token"] = u, tok
            return u
        st.query_params.clear()
    return None


def start_session(username: str, role: str):
    tok = create_session(username)
    st.session_state["user"], st.session_state["token"] = (username, role), tok
    st.query_params["t"] = tok
    st.rerun()


def logout():
    ss = st.session_state
    if ss.get("token"):
        end_session(ss["token"])
    for k in ("user", "token"):
        ss.pop(k, None)
    st.query_params.clear()
    st.rerun()


def login_screen():
    _, mid, _ = st.columns([1, 2, 1])
    with mid:
        brand_header(APP_TITLE, center=True)
        if count_users() == 0:
            st.info("أول تشغيل: أنشئ حساب المدير. احتفظ باسم المستخدم وكلمة المرور.")
            with st.form("setup"):
                u = st.text_input("اسم المستخدم").strip()
                p1 = st.text_input("كلمة المرور", type="password")
                p2 = st.text_input("تأكيد كلمة المرور", type="password")
                if st.form_submit_button("إنشاء الحساب", type="primary"):
                    if not u:
                        st.error("اسم المستخدم مطلوب.")
                    elif len(p1) < MIN_PW:
                        st.error(f"كلمة المرور {MIN_PW} أحرف على الأقل.")
                    elif p1 != p2:
                        st.error("كلمتا المرور غير متطابقتين.")
                    else:
                        create_user(u, p1, "admin")
                        start_session(u, "admin")
        else:
            with st.form("login"):
                u = st.text_input("اسم المستخدم").strip()
                p = st.text_input("كلمة المرور", type="password")
                if st.form_submit_button("دخول", type="primary"):
                    got = check_login(u, p)
                    if got:
                        start_session(*got)
                    else:
                        time.sleep(1)
                        st.error("اسم المستخدم أو كلمة المرور غير صحيحة.")


def account_tab(name: str, is_admin: bool):
    st.subheader("🔑 تغيير كلمة المرور")
    with st.form("chpw", clear_on_submit=True):
        old = st.text_input("كلمة المرور الحالية", type="password")
        n1 = st.text_input("كلمة المرور الجديدة", type="password")
        n2 = st.text_input("تأكيد كلمة المرور الجديدة", type="password")
        if st.form_submit_button("حفظ"):
            if not check_login(name, old):
                st.error("كلمة المرور الحالية غير صحيحة.")
            elif len(n1) < MIN_PW:
                st.error(f"كلمة المرور الجديدة {MIN_PW} أحرف على الأقل.")
            elif n1 != n2:
                st.error("كلمتا المرور غير متطابقتين.")
            else:
                set_password(name, n1)
                st.session_state["token"] = create_session(name)
                st.query_params["t"] = st.session_state["token"]
                st.success("تم تغيير كلمة المرور.")

    if not is_admin:
        return

    st.divider()
    st.subheader("👥 إدارة المستخدمين")
    users = list_users()
    st.dataframe(
        pd.DataFrame([{"المستخدم": u, "الصلاحية": ROLE_AR.get(r, r)} for u, r in users]),
        hide_index=True,
    )
    st.caption("المدير: كل الصلاحيات (حذف السندات، الإعدادات، المستخدمون، إضافةً لما يفعله المستخدم). "
               "المستخدم: إدخال وتعديل وبحث وطباعة واستيراد وتصدير السندات.")

    with st.expander("➕ إضافة مستخدم"):
        with st.form("adduser", clear_on_submit=True):
            u = st.text_input("اسم المستخدم").strip()
            p = st.text_input("كلمة المرور", type="password")
            role = st.selectbox("الصلاحية", ["user", "admin"], format_func=lambda r: ROLE_AR[r])
            if st.form_submit_button("إضافة"):
                if not u:
                    st.error("اسم المستخدم مطلوب.")
                elif len(p) < MIN_PW:
                    st.error(f"كلمة المرور {MIN_PW} أحرف على الأقل.")
                else:
                    try:
                        create_user(u, p, role)
                        st.success(f"تمت إضافة {u}.")
                    except sqlite3.IntegrityError:
                        st.error("اسم المستخدم موجود مسبقاً.")

    names = [u for u, _ in users]
    with st.expander("🔁 إعادة تعيين كلمة مرور مستخدم"):
        with st.form("resetpw", clear_on_submit=True):
            who = st.selectbox("المستخدم", names)
            p = st.text_input("كلمة المرور الجديدة", type="password")
            if st.form_submit_button("تعيين"):
                if len(p) < MIN_PW:
                    st.error(f"كلمة المرور {MIN_PW} أحرف على الأقل.")
                else:
                    set_password(who, p)
                    st.success(f"تم تعيين كلمة مرور جديدة لـ {who}.")
                    if who == name:
                        st.warning("غيّرت كلمة مرورك أنت، سجّل الدخول من جديد.")

    with st.expander("🗑️ حذف مستخدم"):
        others = [u for u in names if u != name]
        who = st.selectbox("المستخدم", others, key="deluser") if others else None
        sure = st.checkbox("أنا متأكد", key="deluser_sure")
        if st.button("حذف المستخدم") and who:
            if sure:
                delete_user(who)
                st.success(f"تم حذف {who}.")
                st.rerun()
            else:
                st.warning("أكّد الحذف أولاً.")


# ------------------------------------------------------------------
# الصفحة الرئيسية
# ------------------------------------------------------------------
def main():
    st.set_page_config(page_title="سندات أفاق", page_icon=page_icon(), layout="wide")
    st.markdown(BRAND_CSS, unsafe_allow_html=True)
    init_db()

    user = current_user()
    if user is None:
        login_screen()
        return
    name, role = user
    is_admin = role == "admin"

    h1, h2 = st.columns([5, 2])
    with h1:
        brand_header(APP_TITLE)
    with h2:
        st.write(f"👤 **{name}** ({ROLE_AR.get(role, role)})")
        if st.button("🚪 تسجيل الخروج"):
            logout()

    tabs = [("➕ إدخال سند", entry_tab), ("📋 السندات والبحث", lambda: view_tab(is_admin)),
            ("✏️ تعديل سند", edit_tab)]
    tabs += [("⬆️ استيراد من Excel", import_tab), ("⬇️ تصدير / نسخ احتياطي", export_tab)]
    if is_admin:
        tabs.append(("⚙️ الإعدادات", print_settings_tab))
    tabs.append(("👤 الحساب والمستخدمون" if is_admin else "👤 حسابي",
                 lambda: account_tab(name, is_admin)))

    for tab, (_, fn) in zip(st.tabs([t[0] for t in tabs]), tabs):
        with tab:
            fn()


if __name__ == "__main__":
    main()
