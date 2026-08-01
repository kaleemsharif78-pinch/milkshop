"""
Doodh Delivery System — NABA TECH BY KALEEM ULLAH SHARIF
Roles: Master Admin, Shop Owner/Admin, Rider, Customer

v7 — Multi-tenant SaaS foundation:
- Master Admin panel: create shops, manage 15-day trial, generate/track
  license/renewal keys, monitor all shops' orders/riders/products in one place
- Licensing: 15-day free trial auto-expires and locks the shop's app; Master
  Admin issues an activation key which the shop admin enters to reactivate
  for another year
- Per-shop dynamic theme (primary color, logo emoji/text) driven from the
  shops table, not hardcoded
- Every shop-scoped table now carries shop_id for tenant isolation
- Existing single-tenant deployments are auto-migrated into a "Default Shop"
  on first run after upgrade, so no data is lost

Kept from earlier versions: encrypted QR, 1-tap no-PIN delivery flow,
multi-product support, rider cash recovery + settlement, in-app
notifications, khata ledger, admin password reset, full timestamp format,
modern self-contained CSS theme.

NOTE — scope for this round: the multi-tenant foundation (this file) is the
prerequisite for two features still to come in a follow-up round:
  1) a customer-facing "extra items" catalog/ordering flow with a live running
     total that notifies rider + admin
  2) PDF invoice / extra-order receipt / delivery summary generation
Both will be built on top of this foundation next.
"""

import streamlit as st
import streamlit.components.v1 as components
import sqlite3
from datetime import datetime, date, timedelta
import hashlib
import io
import os
import re
import base64
import secrets
import string
import pandas as pd

try:
    from reportlab.lib.pagesizes import A4
    from reportlab.lib import colors
    from reportlab.lib.units import mm
    from reportlab.platypus import SimpleDocTemplate, Table, TableStyle, Paragraph, Spacer
    from reportlab.lib.styles import ParagraphStyle
    from reportlab.pdfbase import pdfmetrics
    from reportlab.pdfbase.ttfonts import TTFont
    import arabic_reshaper
    from bidi.algorithm import get_display
    PDF_AVAILABLE = True
except ImportError:
    PDF_AVAILABLE = False

try:
    import qrcode
    QR_AVAILABLE = True
except ImportError:
    QR_AVAILABLE = False

try:
    from pyzbar.pyzbar import decode as qr_decode
    from PIL import Image
    QR_SCAN_AVAILABLE = True
except ImportError:
    QR_SCAN_AVAILABLE = False

try:
    from cryptography.fernet import Fernet, InvalidToken
    CRYPTO_AVAILABLE = True
except ImportError:
    CRYPTO_AVAILABLE = False

DB_PATH = "milk_delivery.db"

UNIT_PRESETS = {
    "kg": [("250g", 0.25), ("500g", 0.5), ("1kg", 1.0)],
    "liter": [("250ml", 0.25), ("500ml", 0.5), ("1L", 1.0)],
    "packet": [("1 پیکٹ", 1), ("2 پیکٹ", 2), ("5 پیکٹ", 5)],
    "piece": [("1", 1), ("2", 2), ("5", 5)],
    "dozen": [("1", 1), ("2", 2), ("5", 5)],
}

DEFAULT_PRODUCTS = [
    ("دودھ (Milk)", "kg", 250.0, 1),
    ("دہی (Yogurt)", "kg", 300.0, 0),
    ("مکھن (Butter)", "kg", 1200.0, 0),
    ("دیسی گھی (Desi Ghee)", "kg", 2500.0, 0),
    ("ملائی (Fresh Cream)", "kg", 600.0, 0),
]


def unit_presets(unit):
    return UNIT_PRESETS.get(unit, UNIT_PRESETS["piece"])


def format_ts(iso_str):
    """[time] - [day] - [date]  e.g. 07:15 AM - Monday - 28 July 2026"""
    if not iso_str:
        return ""
    try:
        dt = datetime.fromisoformat(iso_str)
    except ValueError:
        return iso_str
    return dt.strftime("%I:%M %p - %A - %d %B %Y")


# ----------------------------- DB LAYER -----------------------------

def get_conn():
    conn = sqlite3.connect(DB_PATH, check_same_thread=False)
    conn.row_factory = sqlite3.Row
    conn.execute("PRAGMA foreign_keys = ON")
    return conn


def hash_pw(pw: str) -> str:
    return hashlib.sha256(pw.encode()).hexdigest()


def _add_column_if_missing(conn, table, col_def):
    col_name = col_def.split()[0]
    cols = [r["name"] for r in conn.execute(f"PRAGMA table_info({table})").fetchall()]
    if col_name not in cols:
        conn.execute(f"ALTER TABLE {table} ADD COLUMN {col_def}")


def init_db():
    conn = get_conn()
    c = conn.cursor()

    # ---- shops (tenants) ----
    c.execute("""
        CREATE TABLE IF NOT EXISTS shops (
            id INTEGER PRIMARY KEY AUTOINCREMENT,
            name TEXT NOT NULL,
            status TEXT NOT NULL DEFAULT 'trial' CHECK(status IN ('trial','active','expired','suspended')),
            trial_end_date TEXT,
            license_expires_at TEXT,
            primary_color TEXT DEFAULT '#3B6EA5',
            logo_emoji TEXT DEFAULT '🥛',
            logo_text TEXT DEFAULT 'Doodh Delivery System',
            created_at TEXT NOT NULL
        )
    """)
    _add_column_if_missing(conn, "shops", "proprietor_name TEXT")
    _add_column_if_missing(conn, "shops", "logo_image_base64 TEXT")
    _add_column_if_missing(conn, "shops", "background_color TEXT DEFAULT '#FFFFFF'")
    _add_column_if_missing(conn, "shops", "text_color TEXT DEFAULT '#1F2A37'")
    _add_column_if_missing(conn, "shops", "secondary_color TEXT DEFAULT '#F1F3F5'")
    _add_column_if_missing(conn, "shops", "accent_color TEXT DEFAULT '#5B8FC4'")

    c.execute("""
        CREATE TABLE IF NOT EXISTS broadcast_messages (
            id INTEGER PRIMARY KEY AUTOINCREMENT,
            message TEXT NOT NULL,
            created_at TEXT NOT NULL
        )
    """)

    c.execute("""
        CREATE TABLE IF NOT EXISTS walk_in_sales (
            id INTEGER PRIMARY KEY AUTOINCREMENT,
            shop_id INTEGER NOT NULL,
            total_amount REAL NOT NULL,
            payment_method TEXT DEFAULT 'cash',
            sale_date TEXT NOT NULL,
            timestamp TEXT NOT NULL
        )
    """)

    c.execute("""
        CREATE TABLE IF NOT EXISTS walk_in_sale_items (
            id INTEGER PRIMARY KEY AUTOINCREMENT,
            sale_id INTEGER NOT NULL,
            product_id INTEGER,
            product_name TEXT NOT NULL,
            unit TEXT NOT NULL,
            quantity REAL NOT NULL,
            rate REAL NOT NULL,
            amount REAL NOT NULL,
            FOREIGN KEY(sale_id) REFERENCES walk_in_sales(id)
        )
    """)

    c.execute("""
        CREATE TABLE IF NOT EXISTS farm_supply (
            id INTEGER PRIMARY KEY AUTOINCREMENT,
            shop_id INTEGER NOT NULL,
            supply_date TEXT NOT NULL,
            quantity_kg REAL NOT NULL,
            note TEXT,
            timestamp TEXT NOT NULL
        )
    """)

    c.execute("""
        CREATE TABLE IF NOT EXISTS license_keys (
            id INTEGER PRIMARY KEY AUTOINCREMENT,
            shop_id INTEGER NOT NULL,
            key_code TEXT UNIQUE NOT NULL,
            duration_days INTEGER NOT NULL DEFAULT 365,
            is_used INTEGER DEFAULT 0,
            created_at TEXT NOT NULL,
            used_at TEXT,
            FOREIGN KEY(shop_id) REFERENCES shops(id)
        )
    """)

    c.execute("""
        CREATE TABLE IF NOT EXISTS users (
            id INTEGER PRIMARY KEY AUTOINCREMENT,
            username TEXT UNIQUE NOT NULL,
            password TEXT NOT NULL,
            role TEXT NOT NULL CHECK(role IN ('master_admin','admin','rider','customer')),
            name TEXT NOT NULL,
            phone TEXT,
            active INTEGER DEFAULT 1
        )
    """)
    _add_column_if_missing(conn, "users", "shop_id INTEGER")

    c.execute("""
        CREATE TABLE IF NOT EXISTS customers (
            id INTEGER PRIMARY KEY AUTOINCREMENT,
            user_id INTEGER,
            name TEXT NOT NULL,
            address TEXT,
            phone TEXT,
            code TEXT UNIQUE NOT NULL,
            daily_quota_kg REAL DEFAULT 0,
            active INTEGER DEFAULT 1,
            FOREIGN KEY(user_id) REFERENCES users(id)
        )
    """)
    _add_column_if_missing(conn, "customers", "qr_token TEXT")
    _add_column_if_missing(conn, "customers", "shop_id INTEGER")

    c.execute("""
        CREATE TABLE IF NOT EXISTS riders (
            id INTEGER PRIMARY KEY AUTOINCREMENT,
            user_id INTEGER,
            name TEXT NOT NULL,
            phone TEXT,
            active INTEGER DEFAULT 1,
            FOREIGN KEY(user_id) REFERENCES users(id)
        )
    """)
    _add_column_if_missing(conn, "riders", "shop_id INTEGER")

    c.execute("""
        CREATE TABLE IF NOT EXISTS products (
            id INTEGER PRIMARY KEY AUTOINCREMENT,
            name TEXT NOT NULL,
            unit TEXT NOT NULL DEFAULT 'kg',
            rate REAL NOT NULL DEFAULT 0,
            is_default_quota_item INTEGER DEFAULT 0,
            active INTEGER DEFAULT 1
        )
    """)
    _add_column_if_missing(conn, "products", "shop_id INTEGER")

    c.execute("""
        CREATE TABLE IF NOT EXISTS delivery_txns (
            id INTEGER PRIMARY KEY AUTOINCREMENT,
            customer_id INTEGER NOT NULL,
            rider_id INTEGER NOT NULL,
            delivery_date TEXT NOT NULL,
            status TEXT NOT NULL DEFAULT 'delivered' CHECK(status IN ('delivered','missed')),
            total_amount REAL NOT NULL DEFAULT 0,
            verified_via TEXT,
            timestamp TEXT NOT NULL,
            FOREIGN KEY(customer_id) REFERENCES customers(id),
            FOREIGN KEY(rider_id) REFERENCES riders(id)
        )
    """)
    _add_column_if_missing(conn, "delivery_txns", "shop_id INTEGER")

    c.execute("""
        CREATE TABLE IF NOT EXISTS delivery_items (
            id INTEGER PRIMARY KEY AUTOINCREMENT,
            transaction_id INTEGER NOT NULL,
            product_id INTEGER,
            product_name TEXT NOT NULL,
            unit TEXT NOT NULL,
            quantity REAL NOT NULL,
            rate REAL NOT NULL,
            amount REAL NOT NULL,
            FOREIGN KEY(transaction_id) REFERENCES delivery_txns(id)
        )
    """)

    c.execute("""
        CREATE TABLE IF NOT EXISTS payments (
            id INTEGER PRIMARY KEY AUTOINCREMENT,
            customer_id INTEGER NOT NULL,
            payment_date TEXT NOT NULL,
            amount REAL NOT NULL,
            method TEXT DEFAULT 'cash',
            note TEXT,
            timestamp TEXT NOT NULL,
            FOREIGN KEY(customer_id) REFERENCES customers(id)
        )
    """)

    c.execute("""
        CREATE TABLE IF NOT EXISTS cash_collections (
            id INTEGER PRIMARY KEY AUTOINCREMENT,
            rider_id INTEGER NOT NULL,
            customer_id INTEGER,
            amount REAL NOT NULL,
            note TEXT,
            timestamp TEXT NOT NULL,
            FOREIGN KEY(rider_id) REFERENCES riders(id),
            FOREIGN KEY(customer_id) REFERENCES customers(id)
        )
    """)

    c.execute("""
        CREATE TABLE IF NOT EXISTS cash_settlements (
            id INTEGER PRIMARY KEY AUTOINCREMENT,
            rider_id INTEGER NOT NULL,
            amount REAL NOT NULL,
            note TEXT,
            timestamp TEXT NOT NULL,
            FOREIGN KEY(rider_id) REFERENCES riders(id)
        )
    """)

    c.execute("""
        CREATE TABLE IF NOT EXISTS notifications (
            id INTEGER PRIMARY KEY AUTOINCREMENT,
            customer_id INTEGER,
            audience TEXT NOT NULL CHECK(audience IN ('customer','admin')),
            message TEXT NOT NULL,
            delivery_id INTEGER,
            created_at TEXT NOT NULL,
            is_read INTEGER DEFAULT 0
        )
    """)
    _add_column_if_missing(conn, "notifications", "shop_id INTEGER")

    c.execute("""
        CREATE TABLE IF NOT EXISTS extra_orders (
            id INTEGER PRIMARY KEY AUTOINCREMENT,
            shop_id INTEGER NOT NULL,
            customer_id INTEGER NOT NULL,
            status TEXT NOT NULL DEFAULT 'pending' CHECK(status IN ('pending','fulfilled','cancelled')),
            total_amount REAL NOT NULL DEFAULT 0,
            order_date TEXT NOT NULL,
            timestamp TEXT NOT NULL,
            fulfilled_delivery_id INTEGER
        )
    """)

    c.execute("""
        CREATE TABLE IF NOT EXISTS extra_order_items (
            id INTEGER PRIMARY KEY AUTOINCREMENT,
            order_id INTEGER NOT NULL,
            product_id INTEGER,
            product_name TEXT NOT NULL,
            unit TEXT NOT NULL,
            quantity REAL NOT NULL,
            rate REAL NOT NULL,
            amount REAL NOT NULL,
            FOREIGN KEY(order_id) REFERENCES extra_orders(id)
        )
    """)

    c.execute("""
        CREATE TABLE IF NOT EXISTS settings (
            key TEXT PRIMARY KEY,
            value TEXT
        )
    """)

    conn.commit()

    if CRYPTO_AVAILABLE:
        c.execute("SELECT COUNT(*) AS n FROM settings WHERE key='secret_key'")
        if c.fetchone()["n"] == 0:
            c.execute("INSERT INTO settings (key,value) VALUES (?,?)", ("secret_key", Fernet.generate_key().decode()))
    conn.commit()

    # ---- one-time migration: pre-multi-tenant data -> "Default Shop" ----
    c.execute("SELECT COUNT(*) AS n FROM shops")
    no_shops_yet = c.fetchone()["n"] == 0
    c.execute("SELECT COUNT(*) AS n FROM users WHERE role='admin' AND shop_id IS NULL")
    has_legacy_admin = c.fetchone()["n"] > 0

    if no_shops_yet and has_legacy_admin:
        now = datetime.now()
        cur = c.execute(
            "INSERT INTO shops (name,status,license_expires_at,created_at) VALUES (?,?,?,?)",
            ("My Shop", "active", (now + timedelta(days=365)).isoformat(), now.isoformat())
        )
        default_shop_id = cur.lastrowid
        for tbl in ["users", "customers", "riders", "products", "delivery_txns", "notifications"]:
            c.execute(f"UPDATE {tbl} SET shop_id=? WHERE shop_id IS NULL", (default_shop_id,))
        conn.commit()

    # seed a master admin if none exists yet
    c.execute("SELECT COUNT(*) AS n FROM users WHERE role='master_admin'")
    if c.fetchone()["n"] == 0:
        c.execute(
            "INSERT INTO users (username,password,role,name,phone,shop_id) VALUES (?,?,?,?,?,NULL)",
            ("masteradmin", hash_pw("master123"), "master_admin", "Master Admin", "")
        )
    conn.commit()

    # if there are truly no shops and no legacy admin (fresh install), seed one demo shop + admin
    c.execute("SELECT COUNT(*) AS n FROM shops")
    if c.fetchone()["n"] == 0:
        now = datetime.now()
        cur = c.execute(
            "INSERT INTO shops (name,status,trial_end_date,created_at) VALUES (?,?,?,?)",
            ("Demo Shop", "trial", (now + timedelta(days=15)).isoformat(), now.isoformat())
        )
        demo_shop_id = cur.lastrowid
        c.execute(
            "INSERT INTO users (username,password,role,name,phone,shop_id) VALUES (?,?,?,?,?,?)",
            ("admin", hash_pw("admin123"), "admin", "Owner", "", demo_shop_id)
        )
        c.executemany(
            "INSERT INTO products (name,unit,rate,is_default_quota_item,shop_id) VALUES (?,?,?,?,?)",
            [(n, u, r, d, demo_shop_id) for n, u, r, d in DEFAULT_PRODUCTS]
        )
        conn.commit()

    # backfill products for any shop that somehow has none (defensive)
    for shop in c.execute("SELECT id FROM shops").fetchall():
        cnt = c.execute("SELECT COUNT(*) AS n FROM products WHERE shop_id=?", (shop["id"],)).fetchone()["n"]
        if cnt == 0:
            c.executemany(
                "INSERT INTO products (name,unit,rate,is_default_quota_item,shop_id) VALUES (?,?,?,?,?)",
                [(n, u, r, d, shop["id"]) for n, u, r, d in DEFAULT_PRODUCTS]
            )
    conn.commit()
    conn.close()


def get_setting(key, default=None):
    conn = get_conn()
    row = conn.execute("SELECT value FROM settings WHERE key=?", (key,)).fetchone()
    conn.close()
    return row["value"] if row else default


# ----------------------------- SHOPS / LICENSING -----------------------------

def get_shop(shop_id):
    if not shop_id:
        return None
    conn = get_conn()
    row = conn.execute("SELECT * FROM shops WHERE id=?", (shop_id,)).fetchone()
    conn.close()
    return dict(row) if row else None


def random_key():
    chars = string.ascii_uppercase + string.digits
    return "-".join("".join(secrets.choice(chars) for _ in range(4)) for _ in range(3))


def create_shop(name, admin_username, admin_password, admin_name, trial_days=15):
    conn = get_conn()
    now = datetime.now()
    cur = conn.execute(
        "INSERT INTO shops (name,status,trial_end_date,created_at) VALUES (?,?,?,?)",
        (name, "trial", (now + timedelta(days=trial_days)).isoformat(), now.isoformat())
    )
    shop_id = cur.lastrowid
    conn.execute(
        "INSERT INTO users (username,password,role,name,shop_id) VALUES (?,?,?,?,?)",
        (admin_username, hash_pw(admin_password), "admin", admin_name, shop_id)
    )
    conn.executemany(
        "INSERT INTO products (name,unit,rate,is_default_quota_item,shop_id) VALUES (?,?,?,?,?)",
        [(n, u, r, d, shop_id) for n, u, r, d in DEFAULT_PRODUCTS]
    )
    conn.commit()
    conn.close()
    return shop_id


def generate_license_key(shop_id, duration_days=365):
    key_code = random_key()
    conn = get_conn()
    conn.execute(
        "INSERT INTO license_keys (shop_id,key_code,duration_days,created_at) VALUES (?,?,?,?)",
        (shop_id, key_code, duration_days, datetime.now().isoformat())
    )
    conn.commit()
    conn.close()
    return key_code


def activate_license_key(shop_id, key_code):
    conn = get_conn()
    row = conn.execute(
        "SELECT * FROM license_keys WHERE shop_id=? AND key_code=? AND is_used=0",
        (shop_id, key_code.strip())
    ).fetchone()
    if not row:
        conn.close()
        return False, "غلط یا پہلے سے استعمال شدہ ایکٹیویشن کی۔"
    new_expiry = (datetime.now() + timedelta(days=row["duration_days"])).isoformat()
    conn.execute("UPDATE license_keys SET is_used=1, used_at=? WHERE id=?", (datetime.now().isoformat(), row["id"]))
    conn.execute("UPDATE shops SET status='active', license_expires_at=? WHERE id=?", (new_expiry, shop_id))
    conn.commit()
    conn.close()
    return True, new_expiry


def check_shop_access(shop_id):
    shop = get_shop(shop_id)
    if not shop:
        return False, "شاپ نہیں ملی۔"
    now = datetime.now()
    if shop["status"] == "suspended":
        return False, "یہ اکاؤنٹ ماسٹر ایڈمن کی طرف سے معطل کیا گیا ہے۔"
    if shop["status"] == "trial":
        if shop["trial_end_date"] and datetime.fromisoformat(shop["trial_end_date"]) < now:
            conn = get_conn()
            conn.execute("UPDATE shops SET status='expired' WHERE id=?", (shop_id,))
            conn.commit()
            conn.close()
            return False, "15 دن کا مفت ٹرائل ختم ہو چکا ہے۔"
        return True, None
    if shop["status"] == "active":
        if shop["license_expires_at"] and datetime.fromisoformat(shop["license_expires_at"]) < now:
            conn = get_conn()
            conn.execute("UPDATE shops SET status='expired' WHERE id=?", (shop_id,))
            conn.commit()
            conn.close()
            return False, "لائسنس کی مدت ختم ہو چکی ہے۔"
        return True, None
    return False, "لائسنس/ٹرائل ختم ہو چکا ہے۔ رینیوول کی درکار ہے۔"


# ----------------------------- PRODUCTS -----------------------------

def get_products(shop_id, active_only=True):
    conn = get_conn()
    q = "SELECT * FROM products WHERE shop_id=?"
    params = [shop_id]
    if active_only:
        q += " AND active=1"
    q += " ORDER BY is_default_quota_item DESC, name"
    rows = conn.execute(q, params).fetchall()
    conn.close()
    return [dict(r) for r in rows]


def get_default_quota_product(shop_id):
    conn = get_conn()
    row = conn.execute(
        "SELECT * FROM products WHERE is_default_quota_item=1 AND active=1 AND shop_id=? LIMIT 1", (shop_id,)
    ).fetchone()
    conn.close()
    return dict(row) if row else None


def add_product(shop_id, name, unit, rate):
    conn = get_conn()
    conn.execute("INSERT INTO products (name,unit,rate,shop_id) VALUES (?,?,?,?)", (name, unit, rate, shop_id))
    conn.commit()
    conn.close()


def update_product(product_id, shop_id, name, unit, rate, active):
    conn = get_conn()
    conn.execute(
        "UPDATE products SET name=?, unit=?, rate=?, active=? WHERE id=? AND shop_id=?",
        (name, unit, rate, 1 if active else 0, product_id, shop_id)
    )
    conn.commit()
    conn.close()


# ----------------------------- ENCRYPTION / QR -----------------------------

def get_fernet():
    if not CRYPTO_AVAILABLE:
        return None
    key = get_setting("secret_key")
    return Fernet(key.encode())


def generate_customer_qr_token(customer_id: int) -> str:
    f = get_fernet()
    token = f.encrypt(str(customer_id).encode()).decode() if f else None
    conn = get_conn()
    if token:
        conn.execute("UPDATE customers SET qr_token=? WHERE id=?", (token, customer_id))
        conn.commit()
    conn.close()
    return token


def verify_customer_qr(scanned_data: str, shop_id: int):
    """Mirrors POST /verify-customer-qr — scoped to the scanning rider's shop
    so a QR from a different tenant can never match."""
    conn = get_conn()
    f = get_fernet()
    if f is not None:
        try:
            customer_id = int(f.decrypt(scanned_data.encode()).decode())
            row = conn.execute("SELECT * FROM customers WHERE id=? AND active=1 AND shop_id=?", (customer_id, shop_id)).fetchone()
            if row:
                conn.close()
                return dict(row)
        except (InvalidToken, ValueError, Exception):
            pass
    row = conn.execute("SELECT * FROM customers WHERE code=? AND active=1 AND shop_id=?", (scanned_data, shop_id)).fetchone()
    conn.close()
    return dict(row) if row else None


# ----------------------------- NOTIFICATIONS -----------------------------

def push_notification(customer_id, message, shop_id, audience="customer", delivery_id=None):
    conn = get_conn()
    conn.execute(
        "INSERT INTO notifications (customer_id,audience,message,delivery_id,created_at,shop_id) VALUES (?,?,?,?,?,?)",
        (customer_id, audience, message, delivery_id, datetime.now().isoformat(), shop_id)
    )
    conn.commit()
    conn.close()


def get_notifications(audience, shop_id, customer_id=None, limit=20):
    conn = get_conn()
    if audience == "customer":
        rows = conn.execute(
            "SELECT * FROM notifications WHERE audience='customer' AND customer_id=? AND shop_id=? ORDER BY created_at DESC LIMIT ?",
            (customer_id, shop_id, limit)
        ).fetchall()
    else:
        rows = conn.execute(
            "SELECT n.*, c.name AS customer_name FROM notifications n "
            "LEFT JOIN customers c ON c.id=n.customer_id "
            "WHERE n.audience='admin' AND n.shop_id=? ORDER BY n.created_at DESC LIMIT ?",
            (shop_id, limit)
        ).fetchall()
    conn.close()
    return [dict(r) for r in rows]


# ----------------------------- MASTER ADMIN BROADCASTS -----------------------------

def post_broadcast(message):
    """Master Admin sends a greeting/update that every user of every shop sees."""
    conn = get_conn()
    conn.execute(
        "INSERT INTO broadcast_messages (message,created_at) VALUES (?,?)",
        (message, datetime.now().isoformat())
    )
    conn.commit()
    conn.close()


def get_recent_broadcasts(limit=3):
    conn = get_conn()
    rows = conn.execute(
        "SELECT * FROM broadcast_messages ORDER BY created_at DESC LIMIT ?", (limit,)
    ).fetchall()
    conn.close()
    return [dict(r) for r in rows]


# ----------------------------- WALK-IN CUSTOMER / POS -----------------------------

def record_walk_in_sale(shop_id, cart_items, payment_method="cash"):
    """Counter cash sale for a walk-in (non-subscription) customer."""
    total_amount = round(sum(it["qty"] * it["rate"] for it in cart_items), 2)
    conn = get_conn()
    cur = conn.execute(
        "INSERT INTO walk_in_sales (shop_id,total_amount,payment_method,sale_date,timestamp) VALUES (?,?,?,?,?)",
        (shop_id, total_amount, payment_method, date.today().isoformat(), datetime.now().isoformat())
    )
    sale_id = cur.lastrowid
    for it in cart_items:
        amount = round(it["qty"] * it["rate"], 2)
        conn.execute(
            "INSERT INTO walk_in_sale_items (sale_id,product_id,product_name,unit,quantity,rate,amount) VALUES (?,?,?,?,?,?,?)",
            (sale_id, it.get("product_id"), it["product_name"], it["unit"], it["qty"], it["rate"], amount)
        )
    conn.commit()
    conn.close()
    return sale_id, total_amount


def get_walk_in_sales(shop_id, date_filter=None, month_filter=None):
    q = (
        "SELECT ws.*, GROUP_CONCAT(wsi.product_name || ' ' || wsi.quantity || wsi.unit, ', ') AS items_summary "
        "FROM walk_in_sales ws LEFT JOIN walk_in_sale_items wsi ON wsi.sale_id=ws.id "
        "WHERE ws.shop_id=?"
    )
    params = [shop_id]
    if date_filter:
        q += " AND ws.sale_date=?"
        params.append(date_filter)
    if month_filter:
        q += " AND ws.sale_date LIKE ?"
        params.append(f"{month_filter}%")
    q += " GROUP BY ws.id ORDER BY ws.timestamp DESC"
    conn = get_conn()
    rows = conn.execute(q, params).fetchall()
    conn.close()
    return [dict(r) for r in rows]


def get_walk_in_sale_items(sale_id):
    conn = get_conn()
    rows = conn.execute("SELECT * FROM walk_in_sale_items WHERE sale_id=?", (sale_id,)).fetchall()
    conn.close()
    return [dict(r) for r in rows]


# ----------------------------- BAARA / FARM SUPPLY -----------------------------

def record_farm_supply(shop_id, supply_date, quantity_kg, note=""):
    conn = get_conn()
    conn.execute(
        "INSERT INTO farm_supply (shop_id,supply_date,quantity_kg,note,timestamp) VALUES (?,?,?,?,?)",
        (shop_id, supply_date, quantity_kg, note, datetime.now().isoformat())
    )
    conn.commit()
    conn.close()


def get_farm_supply(shop_id, date_filter=None, month_filter=None):
    q = "SELECT * FROM farm_supply WHERE shop_id=?"
    params = [shop_id]
    if date_filter:
        q += " AND supply_date=?"
        params.append(date_filter)
    if month_filter:
        q += " AND supply_date LIKE ?"
        params.append(f"{month_filter}%")
    q += " ORDER BY supply_date DESC"
    conn = get_conn()
    rows = conn.execute(q, params).fetchall()
    conn.close()
    return [dict(r) for r in rows]


def get_daily_reconciliation(shop_id, day):
    """Farm supply vs. subscription deliveries vs. walk-in sales, for the milk product only."""
    conn = get_conn()
    farm_total = conn.execute(
        "SELECT COALESCE(SUM(quantity_kg),0) AS s FROM farm_supply WHERE shop_id=? AND supply_date=?",
        (shop_id, day)
    ).fetchone()["s"]

    milk = get_default_quota_product(shop_id)
    subscription_used = 0.0
    walkin_sold = 0.0
    if milk:
        subscription_used = conn.execute(
            "SELECT COALESCE(SUM(di.quantity),0) AS s FROM delivery_items di "
            "JOIN delivery_txns dt ON dt.id=di.transaction_id "
            "WHERE dt.shop_id=? AND dt.delivery_date=? AND dt.status='delivered' AND di.product_name=?",
            (shop_id, day, milk["name"])
        ).fetchone()["s"]
        walkin_sold = conn.execute(
            "SELECT COALESCE(SUM(wsi.quantity),0) AS s FROM walk_in_sale_items wsi "
            "JOIN walk_in_sales ws ON ws.id=wsi.sale_id "
            "WHERE ws.shop_id=? AND ws.sale_date=? AND wsi.product_name=?",
            (shop_id, day, milk["name"])
        ).fetchone()["s"]
    conn.close()

    remaining = round(farm_total - subscription_used - walkin_sold, 2)
    return {
        "farm_total": farm_total, "subscription_used": subscription_used,
        "walkin_sold": walkin_sold, "remaining": remaining, "milk_product": milk["name"] if milk else None
    }


# ----------------------------- RIDER CASH RECOVERY -----------------------------

def rider_cash_in_hand(rider_id: int) -> float:
    conn = get_conn()
    collected = conn.execute(
        "SELECT COALESCE(SUM(amount),0) AS s FROM cash_collections WHERE rider_id=?", (rider_id,)
    ).fetchone()["s"]
    settled = conn.execute(
        "SELECT COALESCE(SUM(amount),0) AS s FROM cash_settlements WHERE rider_id=?", (rider_id,)
    ).fetchone()["s"]
    conn.close()
    return round(collected - settled, 2)


def record_cash_collection(rider_id: int, customer_id, amount: float, shop_id, note: str = ""):
    conn = get_conn()
    conn.execute(
        "INSERT INTO cash_collections (rider_id,customer_id,amount,note,timestamp) VALUES (?,?,?,?,?)",
        (rider_id, customer_id, amount, note, datetime.now().isoformat())
    )
    if customer_id:
        conn.execute(
            "INSERT INTO payments (customer_id,payment_date,amount,method,note,timestamp) VALUES (?,?,?,?,?,?)",
            (customer_id, date.today().isoformat(), amount, "cash", note or "رائیڈر نے موقع پر وصول کیا", datetime.now().isoformat())
        )
    conn.commit()
    conn.close()
    if customer_id:
        push_notification(customer_id, f"آپ کی Rs {amount:.0f} نقد وصولی رائیڈر کے ذریعے درج ہو گئی", shop_id, audience="customer")


def settle_rider_cash(rider_id: int, amount: float, note: str = ""):
    conn = get_conn()
    conn.execute(
        "INSERT INTO cash_settlements (rider_id,amount,note,timestamp) VALUES (?,?,?,?)",
        (rider_id, amount, note, datetime.now().isoformat())
    )
    conn.commit()
    conn.close()


# ----------------------------- DELIVERY (CONFIRM) -----------------------------

def items_summary_text(cart_items):
    return ", ".join(f"{it['product_name']} {it['qty']}{it['unit']}" for it in cart_items)


def confirm_delivery(customer_id, rider_id, cart_items, shop_id, status="delivered"):
    total_amount = round(sum(it["qty"] * it["rate"] for it in cart_items), 2)
    conn = get_conn()
    cur = conn.execute(
        "INSERT INTO delivery_txns (customer_id,rider_id,delivery_date,status,total_amount,verified_via,timestamp,shop_id) "
        "VALUES (?,?,?,?,?,?,?,?)",
        (customer_id, rider_id, date.today().isoformat(), status, total_amount, "QR Scan", datetime.now().isoformat(), shop_id)
    )
    txn_id = cur.lastrowid
    for it in cart_items:
        amount = round(it["qty"] * it["rate"], 2)
        conn.execute(
            "INSERT INTO delivery_items (transaction_id,product_id,product_name,unit,quantity,rate,amount) VALUES (?,?,?,?,?,?,?)",
            (txn_id, it.get("product_id"), it["product_name"], it["unit"], it["qty"], it["rate"], amount)
        )
    conn.commit()
    conn.close()

    if status == "delivered" and cart_items:
        summary = items_summary_text(cart_items)
        push_notification(customer_id, f"آپ کے ہاں {summary} ڈیلیور ہوا — رقم Rs {total_amount:.0f}", shop_id, audience="customer", delivery_id=txn_id)
        push_notification(customer_id, f"ڈیلیوری کنفرم ہوئی — {summary} / Rs {total_amount:.0f}", shop_id, audience="admin", delivery_id=txn_id)
    return txn_id


def mark_missed(customer_id, rider_id, shop_id):
    conn = get_conn()
    cur = conn.execute(
        "INSERT INTO delivery_txns (customer_id,rider_id,delivery_date,status,total_amount,verified_via,timestamp,shop_id) "
        "VALUES (?,?,?,?,?,?,?,?)",
        (customer_id, rider_id, date.today().isoformat(), "missed", 0, "n/a", datetime.now().isoformat(), shop_id)
    )
    txn_id = cur.lastrowid
    conn.commit()
    conn.close()
    push_notification(customer_id, "آج ڈیلیوری نہیں ہوئی (ناغہ درج)", shop_id, audience="customer", delivery_id=txn_id)
    push_notification(customer_id, "ناغہ درج ہوا", shop_id, audience="admin", delivery_id=txn_id)


def get_transactions(shop_id, customer_id=None, rider_id=None, date_filter=None, month_filter=None):
    q = (
        "SELECT dt.*, c.name AS customer_name, r.name AS rider_name, "
        "GROUP_CONCAT(di.product_name || ' ' || di.quantity || di.unit, ', ') AS items_summary "
        "FROM delivery_txns dt "
        "JOIN customers c ON c.id=dt.customer_id "
        "JOIN riders r ON r.id=dt.rider_id "
        "LEFT JOIN delivery_items di ON di.transaction_id=dt.id "
        "WHERE dt.shop_id=?"
    )
    params = [shop_id]
    if customer_id:
        q += " AND dt.customer_id=?"
        params.append(customer_id)
    if rider_id:
        q += " AND dt.rider_id=?"
        params.append(rider_id)
    if date_filter:
        q += " AND dt.delivery_date=?"
        params.append(date_filter)
    if month_filter:
        q += " AND dt.delivery_date LIKE ?"
        params.append(f"{month_filter}%")
    q += " GROUP BY dt.id ORDER BY dt.timestamp DESC"
    conn = get_conn()
    rows = conn.execute(q, params).fetchall()
    conn.close()
    return [dict(r) for r in rows]


# ----------------------------- EXTRA ORDERS (customer self-service catalog) -----------------------------

def place_extra_order(shop_id, customer_id, cart_items):
    """Customer submits an ad-hoc order for extra items (beyond the daily milk
    subscription). Notifies both the customer (confirmation) and admin/rider
    (so the rider brings the right items on the next delivery)."""
    total_amount = round(sum(it["qty"] * it["rate"] for it in cart_items), 2)
    conn = get_conn()
    cur = conn.execute(
        "INSERT INTO extra_orders (shop_id,customer_id,status,total_amount,order_date,timestamp) VALUES (?,?,?,?,?,?)",
        (shop_id, customer_id, "pending", total_amount, date.today().isoformat(), datetime.now().isoformat())
    )
    order_id = cur.lastrowid
    for it in cart_items:
        amount = round(it["qty"] * it["rate"], 2)
        conn.execute(
            "INSERT INTO extra_order_items (order_id,product_id,product_name,unit,quantity,rate,amount) VALUES (?,?,?,?,?,?,?)",
            (order_id, it.get("product_id"), it["product_name"], it["unit"], it["qty"], it["rate"], amount)
        )
    conn.commit()
    conn.close()

    summary = items_summary_text(cart_items)
    push_notification(customer_id, f"آپ کا اضافی آرڈر جمع ہو گیا — {summary} / Rs {total_amount:.0f}", shop_id, audience="customer")
    push_notification(customer_id, f"نیا اضافی آرڈر موصول ہوا — {summary} / Rs {total_amount:.0f}", shop_id, audience="admin")
    return order_id


def get_extra_orders(shop_id, customer_id=None, status=None):
    q = (
        "SELECT eo.*, c.name AS customer_name, "
        "GROUP_CONCAT(eoi.product_name || ' ' || eoi.quantity || eoi.unit, ', ') AS items_summary "
        "FROM extra_orders eo JOIN customers c ON c.id=eo.customer_id "
        "LEFT JOIN extra_order_items eoi ON eoi.order_id=eo.id "
        "WHERE eo.shop_id=?"
    )
    params = [shop_id]
    if customer_id:
        q += " AND eo.customer_id=?"
        params.append(customer_id)
    if status:
        q += " AND eo.status=?"
        params.append(status)
    q += " GROUP BY eo.id ORDER BY eo.timestamp DESC"
    conn = get_conn()
    rows = conn.execute(q, params).fetchall()
    conn.close()
    return [dict(r) for r in rows]


def get_extra_order_items(order_id):
    conn = get_conn()
    rows = conn.execute("SELECT * FROM extra_order_items WHERE order_id=?", (order_id,)).fetchall()
    conn.close()
    return [dict(r) for r in rows]


def fulfill_extra_order(order_id, delivery_txn_id):
    conn = get_conn()
    conn.execute(
        "UPDATE extra_orders SET status='fulfilled', fulfilled_delivery_id=? WHERE id=?",
        (delivery_txn_id, order_id)
    )
    conn.commit()
    conn.close()


# ----------------------------- AUTH -----------------------------

def authenticate(username, password):
    conn = get_conn()
    row = conn.execute(
        "SELECT * FROM users WHERE username=? AND active=1", (username,)
    ).fetchone()
    conn.close()
    if row and row["password"] == hash_pw(password):
        return dict(row)
    return None


def random_password(length=6):
    return "".join(secrets.choice(string.digits) for _ in range(length))


def reset_user_password(user_id, new_password):
    conn = get_conn()
    conn.execute("UPDATE users SET password=? WHERE id=?", (hash_pw(new_password), user_id))
    conn.commit()
    conn.close()


def login_page():
    with st.form("login_form"):
        username = st.text_input("Username")
        password = st.text_input("Password", type="password")
        submitted = st.form_submit_button("Login", use_container_width=True)
        if submitted:
            user = authenticate(username.strip(), password)
            if user:
                st.session_state.user = user
                st.rerun()
            else:
                st.error("غلط یوزرنیم یا پاسورڈ")


def logout_button():
    with st.sidebar:
        st.markdown(f"**{st.session_state.user['name']}**")
        st.caption(st.session_state.user['role'].upper())
        if st.button("Logout", use_container_width=True):
            del st.session_state.user
            st.rerun()


# ----------------------------- SHARED HELPERS -----------------------------

def get_customers(shop_id, active_only=True):
    conn = get_conn()
    q = "SELECT * FROM customers WHERE shop_id=?"
    params = [shop_id]
    if active_only:
        q += " AND active=1"
    q += " ORDER BY name"
    rows = conn.execute(q, params).fetchall()
    conn.close()
    return [dict(r) for r in rows]


def get_rider_by_user(user_id):
    conn = get_conn()
    row = conn.execute("SELECT * FROM riders WHERE user_id=?", (user_id,)).fetchone()
    conn.close()
    return dict(row) if row else None


def customer_balance(customer_id):
    conn = get_conn()
    total_amount = conn.execute(
        "SELECT COALESCE(SUM(total_amount),0) AS s FROM delivery_txns WHERE customer_id=? AND status!='missed'",
        (customer_id,)
    ).fetchone()["s"]
    total_paid = conn.execute(
        "SELECT COALESCE(SUM(amount),0) AS s FROM payments WHERE customer_id=?",
        (customer_id,)
    ).fetchone()["s"]
    conn.close()
    return round(total_amount - total_paid, 2)


# ----------------------------- THEME / BRANDING -----------------------------

def apply_theme(shop=None):
    primary = (shop.get("primary_color") if shop else None) or "#3B6EA5"
    bg = (shop.get("background_color") if shop else None) or "#FFFFFF"
    text_color = (shop.get("text_color") if shop else None) or "#1F2A37"
    secondary = (shop.get("secondary_color") if shop else None) or "#F1F3F5"
    accent = (shop.get("accent_color") if shop else None) or "#5B8FC4"
    st.markdown(f"""
    <style>
        @import url('https://fonts.googleapis.com/css2?family=Poppins:wght@400;500;600;700&display=swap');

        html, body, .stApp, [data-testid="stAppViewContainer"], [data-testid="stMain"] {{
            background-color: {bg} !important;
            color: {text_color} !important;
            font-family: 'Poppins', sans-serif !important;
        }}
        [data-testid="stHeader"] {{ background-color: {bg} !important; }}
        [data-testid="stSidebar"] {{ background-color: {secondary} !important; }}

        .naba-banner {{
            background: linear-gradient(90deg, {primary}CC 0%, {primary} 55%, {accent} 100%);
            padding: 18px 24px;
            border-radius: 14px;
            margin-bottom: 18px;
            box-shadow: 0 4px 14px rgba(0,0,0,0.15);
        }}
        .naba-banner-row {{ display: flex; align-items: center; gap: 14px; }}
        .naba-banner-logo-img {{ height: 46px; width: 46px; object-fit: contain; border-radius: 8px; background: #FFFFFF; padding: 3px; }}
        .naba-banner h1 {{ color: #FFFFFF !important; margin: 0; font-size: 26px; font-weight: 700; }}
        .naba-banner p.proprietor {{ color: #EAF0FA !important; margin: 2px 0 0 0; font-size: 14px; font-weight: 500; }}
        .naba-banner p.contact {{ color: #EAF0FA !important; opacity: 0.75; margin: 4px 0 0 0; font-size: 10px; }}

        [data-testid="stMetric"] {{
            background-color: {secondary} !important;
            padding: 14px !important;
            border-radius: 14px !important;
            border: 1px solid #E6E9EE !important;
            box-shadow: 0 2px 6px rgba(31,42,55,0.05);
        }}
        [data-testid="stMetricLabel"] {{ color: #6B7280 !important; }}
        [data-testid="stMetricValue"] {{ color: {text_color} !important; font-weight: 600; }}
        [data-testid="stExpander"] {{
            border-radius: 12px !important;
            border: 1px solid #E6E9EE !important;
            box-shadow: 0 2px 6px rgba(31,42,55,0.04);
        }}

        h1, h2, h3, h4, h5 {{ color: {text_color}; font-weight: 600; }}
        input, textarea, [data-baseweb="select"] > div {{ border-radius: 10px !important; }}

        .stButton > button {{
            border-radius: 10px !important;
            border-color: {primary} !important;
            color: {primary} !important;
            font-weight: 500;
            transition: all 0.15s ease-in-out;
        }}
        .stButton > button:hover {{
            background-color: {primary}1A !important;
            transform: translateY(-1px);
        }}
        .stButton > button[kind="primary"] {{
            background-color: {primary} !important;
            border-color: {primary} !important;
            color: #FFFFFF !important;
            box-shadow: 0 3px 8px rgba(0,0,0,0.2);
        }}
        [data-testid="stTabs"] button[aria-selected="true"] {{
            color: {primary} !important;
            border-bottom-color: {primary} !important;
            font-weight: 600;
        }}
        [data-baseweb="tab-highlight"] {{ background-color: {primary} !important; }}
        [data-baseweb="tab-list"] {{ border-bottom-color: #E2E5E9 !important; }}
        a, a:visited {{ color: {primary} !important; }}
    </style>
    """, unsafe_allow_html=True)


def render_banner(shop=None):
    emoji = (shop.get("logo_emoji") if shop else None) or "🥛"
    text = (shop.get("logo_text") if shop else None) or "Doodh Delivery System"
    proprietor = shop.get("proprietor_name") if shop else None
    logo_img = shop.get("logo_image_base64") if shop else None

    logo_html = f'<img class="naba-banner-logo-img" src="data:image/png;base64,{logo_img}" />' if logo_img else f'<span style="font-size:34px;">{emoji}</span>'
    proprietor_html = f'<p class="proprietor">{proprietor}</p>' if proprietor else ""

    # Built as ONE unbroken line (no newlines/indentation). Streamlit's
    # st.markdown runs standard Markdown first: a blank line followed by
    # indented text gets turned into a literal code block, which broke the
    # HTML whenever proprietor_html was empty (leaving a blank line mid-block).
    banner_html = (
        '<div class="naba-banner">'
        '<div class="naba-banner-row">'
        f'{logo_html}'
        '<div>'
        f'<h1>{text}</h1>'
        f'{proprietor_html}'
        '</div></div>'
        '<p class="contact">NABA Tech | Mobile: 03151186003</p>'
        '</div>'
    )
    st.markdown(banner_html, unsafe_allow_html=True)


def render_broadcasts():
    """Shows the Master Admin's latest greeting/update messages to every user."""
    broadcasts = get_recent_broadcasts(limit=3)
    for b in broadcasts:
        st.info(f"📢 {b['message']}")


# ----------------------------- LICENSE LOCK SCREEN -----------------------------

def license_lock_screen(user):
    shop = get_shop(user["shop_id"])
    _, reason = check_shop_access(user["shop_id"])
    st.error(f"🔒 {shop['name'] if shop else 'یہ شاپ'} کی رسائی بند ہے۔")
    if reason:
        st.warning(reason)

    if user["role"] == "admin":
        st.subheader("🔑 ایکٹیویشن کی درج کریں")
        st.caption("یہ کی آپ کو NABA TECH / ماسٹر ایڈمن کی طرف سے فراہم کی جائے گی۔")
        key_input = st.text_input("Activation Key")
        if st.button("✅ ری ایکٹیویٹ کریں", type="primary"):
            ok, result = activate_license_key(user["shop_id"], key_input)
            if ok:
                st.success(f"سافٹ ویئر دوبارہ فعال ہو گیا! میعاد: {format_ts(result)} تک۔")
                st.rerun()
            else:
                st.error(result)
    else:
        st.info("براہ کرم اپنی شاپ کے ایڈمن سے رابطہ کریں تاکہ لائسنس رینیو ہو سکے۔")


# ----------------------------- PDF GENERATION -----------------------------

_FONTS_DIR = os.path.join(os.path.dirname(os.path.abspath(__file__)), "fonts")
_URDU_FONTS_REGISTERED = False


def _ensure_urdu_fonts():
    """Registers bundled Urdu-capable TTF fonts once. Falls back to
    Helvetica (no Urdu glyphs) if the fonts folder isn't shipped alongside
    the script or reportlab/font libs aren't installed."""
    global _URDU_FONTS_REGISTERED
    if not PDF_AVAILABLE or _URDU_FONTS_REGISTERED:
        return
    try:
        pdfmetrics.registerFont(TTFont("UrduHeading", os.path.join(_FONTS_DIR, "NotoNastaliqUrdu.ttf")))
        pdfmetrics.registerFont(TTFont("UrduBody", os.path.join(_FONTS_DIR, "NotoNaskhArabic.ttf")))
        _URDU_FONTS_REGISTERED = True
    except Exception:
        _URDU_FONTS_REGISTERED = False


_ARABIC_RE = re.compile(r"[\u0600-\u06FF\u0750-\u077F]")


def has_arabic(text):
    return bool(_ARABIC_RE.search(text or ""))


def pick_font(text, heading=False):
    """Only route text through the Urdu fonts when it actually contains
    Arabic/Urdu characters. Nastaliq in particular has unusually tall line
    metrics — using it for pure-Latin strings (like an English shop name)
    caused headings to visually overlap the line below them."""
    if has_arabic(text) and _URDU_FONTS_REGISTERED:
        return "UrduHeading" if heading else "UrduBody"
    return "Helvetica-Bold" if heading else "Helvetica"


def ur(text):
    """Reshape + bidi-reorder Urdu/Arabic text for correct RTL rendering in
    reportlab. Safe no-op on pure Latin/numeric text (Rs amounts, dates)."""
    if text is None:
        return ""
    text = str(text)
    try:
        return get_display(arabic_reshaper.reshape(text))
    except Exception:
        return text


def _pcell(text, font_size=9, color="#1F2A37", bold=False):
    """Build a table cell as a Paragraph (not a raw string) so long text
    (like a joined product list) wraps onto multiple lines with proper
    leading — raw strings in reportlab Tables can wrap without adding line
    spacing, causing wrapped lines to visually overlap."""
    text = "" if text is None else str(text)
    font = pick_font(text, heading=bold)
    style = ParagraphStyle(
        f"Cell_{font}_{font_size}_{color}",
        fontName=font, fontSize=font_size, leading=font_size * 1.6,
        alignment=2, textColor=colors.HexColor(color),
    )
    return Paragraph(ur(text), style)


def _pdf_header(elements, shop, title):
    _ensure_urdu_fonts()
    primary = (shop.get("primary_color") if shop else None) or "#3B6EA5"
    logo_text = (shop.get("logo_text") if shop else None) or "Doodh Delivery System"

    title_font = pick_font(logo_text, heading=True)
    title_style = ParagraphStyle("TitleUr", fontName=title_font, fontSize=20, leading=32, alignment=2, textColor=colors.HexColor(primary))
    sub_style = ParagraphStyle("SubUr", fontName="Helvetica", fontSize=10, leading=14, alignment=2, textColor=colors.HexColor("#4B5563"))
    h2_font = pick_font(title, heading=True)
    h2_style = ParagraphStyle("H2Ur", fontName=h2_font, fontSize=15, leading=26, alignment=2, textColor=colors.HexColor("#1F2A37"))

    default_body_font = "UrduBody" if _URDU_FONTS_REGISTERED else "Helvetica"

    elements.append(Paragraph(ur(logo_text), title_style))
    elements.append(Spacer(1, 6))
    elements.append(Paragraph(ur("NABA TECH BY KALEEM ULLAH SHARIF"), sub_style))
    elements.append(Spacer(1, 16))
    elements.append(Paragraph(ur(title), h2_style))
    elements.append(Spacer(1, 10))
    return default_body_font


def generate_invoice_pdf(shop, customer, month, transactions, total_bill, total_paid, balance):
    """Monthly invoice / bill PDF for a customer."""
    buf = io.BytesIO()
    doc = SimpleDocTemplate(buf, pagesize=A4, topMargin=24 * mm, bottomMargin=20 * mm, leftMargin=18 * mm, rightMargin=18 * mm)
    elements = []
    body_font = _pdf_header(elements, shop, f"ماہانہ انوائس — {month}")

    info_style = ParagraphStyle("InfoUr", fontName=body_font, fontSize=11, leading=17, alignment=2, textColor=colors.HexColor("#1F2A37"))
    elements.append(Paragraph(ur(f"کسٹمر: {customer['name']}"), info_style))
    if customer.get("address"):
        elements.append(Paragraph(ur(f"پتہ: {customer['address']}"), info_style))
    if customer.get("phone"):
        elements.append(Paragraph(ur(f"فون: {customer['phone']}"), info_style))
    elements.append(Spacer(1, 12))

    data = [[_pcell("اسٹیٹس", color="#FFFFFF"), _pcell("رقم", color="#FFFFFF"), _pcell("پروڈکٹس", color="#FFFFFF"), _pcell("تاریخ/وقت", color="#FFFFFF")]]
    for r in transactions:
        data.append([_pcell(r["status"]), _pcell(f"Rs {r['total_amount']:.0f}"), _pcell(r["items_summary"] or "—"), _pcell(format_ts(r["timestamp"]))])
    table = Table(data, colWidths=[25 * mm, 25 * mm, 60 * mm, 45 * mm])
    table.setStyle(TableStyle([
        ("BACKGROUND", (0, 0), (-1, 0), colors.HexColor((shop.get("primary_color") if shop else None) or "#3B6EA5")),
        ("GRID", (0, 0), (-1, -1), 0.5, colors.grey),
        ("ALIGN", (0, 0), (-1, -1), "RIGHT"),
        ("VALIGN", (0, 0), (-1, -1), "MIDDLE"),
        ("TOPPADDING", (0, 0), (-1, -1), 6),
        ("BOTTOMPADDING", (0, 0), (-1, -1), 6),
        ("ROWBACKGROUNDS", (0, 1), (-1, -1), [colors.white, colors.HexColor("#F8F9FB")]),
    ]))
    elements.append(table)
    elements.append(Spacer(1, 16))

    total_style = ParagraphStyle("TotalUr", fontName=pick_font("کل بل"), fontSize=12, leading=18, alignment=2, textColor=colors.HexColor("#1F2A37"))
    bold_style = ParagraphStyle("BoldUr", fontName=pick_font("باقی بقیہ", heading=True), fontSize=14, leading=24, alignment=2, textColor=colors.HexColor("#1F2A37"))
    elements.append(Paragraph(ur(f"کل بل: Rs {total_bill:.0f}"), total_style))
    elements.append(Spacer(1, 4))
    elements.append(Paragraph(ur(f"وصول شدہ: Rs {total_paid:.0f}"), total_style))
    elements.append(Spacer(1, 8))
    elements.append(Paragraph(ur(f"باقی بقیہ: Rs {balance:.0f}"), bold_style))

    doc.build(elements)
    buf.seek(0)
    return buf


def generate_extra_order_receipt_pdf(shop, customer, order, items):
    """Receipt PDF for a single extra (ad-hoc) order."""
    buf = io.BytesIO()
    doc = SimpleDocTemplate(buf, pagesize=A4, topMargin=24 * mm, bottomMargin=20 * mm, leftMargin=18 * mm, rightMargin=18 * mm)
    elements = []
    body_font = _pdf_header(elements, shop, "اضافی آرڈر کی رسید")

    info_style = ParagraphStyle("InfoUr2", fontName=body_font, fontSize=11, leading=17, alignment=2, textColor=colors.HexColor("#1F2A37"))
    elements.append(Paragraph(ur(f"کسٹمر: {customer['name']}"), info_style))
    elements.append(Paragraph(ur(f"آرڈر نمبر: #{order['id']}"), info_style))
    elements.append(Paragraph(ur(f"تاریخ: {format_ts(order['timestamp'])}"), info_style))
    elements.append(Paragraph(ur(f"اسٹیٹس: {order['status']}"), info_style))
    elements.append(Spacer(1, 12))

    data = [[_pcell("رقم", color="#FFFFFF"), _pcell("ریٹ", color="#FFFFFF"), _pcell("مقدار", color="#FFFFFF"), _pcell("آئٹم", color="#FFFFFF")]]
    for it in items:
        data.append([_pcell(f"Rs {it['amount']:.0f}"), _pcell(f"Rs {it['rate']:.0f}"), _pcell(f"{it['quantity']}{it['unit']}"), _pcell(it["product_name"])])
    table = Table(data, colWidths=[25 * mm, 25 * mm, 25 * mm, 80 * mm])
    table.setStyle(TableStyle([
        ("BACKGROUND", (0, 0), (-1, 0), colors.HexColor((shop.get("primary_color") if shop else None) or "#3B6EA5")),
        ("GRID", (0, 0), (-1, -1), 0.5, colors.grey),
        ("ALIGN", (0, 0), (-1, -1), "RIGHT"),
        ("VALIGN", (0, 0), (-1, -1), "MIDDLE"),
        ("TOPPADDING", (0, 0), (-1, -1), 6),
        ("BOTTOMPADDING", (0, 0), (-1, -1), 6),
    ]))
    elements.append(table)
    elements.append(Spacer(1, 14))
    bold_style = ParagraphStyle("BoldUr2", fontName=pick_font("کل رقم", heading=True), fontSize=14, leading=24, alignment=2, textColor=colors.HexColor("#1F2A37"))
    elements.append(Paragraph(ur(f"کل رقم: Rs {order['total_amount']:.0f}"), bold_style))

    doc.build(elements)
    buf.seek(0)
    return buf


def generate_delivery_summary_pdf(shop, rows, period_label):
    """Daily/monthly delivery summary PDF (admin use) across all customers."""
    buf = io.BytesIO()
    doc = SimpleDocTemplate(buf, pagesize=A4, topMargin=24 * mm, bottomMargin=20 * mm, leftMargin=18 * mm, rightMargin=18 * mm)
    elements = []
    body_font = _pdf_header(elements, shop, f"ڈیلیوری سمری — {period_label}")

    data = [[_pcell("اسٹیٹس", color="#FFFFFF"), _pcell("رقم", color="#FFFFFF"), _pcell("پروڈکٹس", color="#FFFFFF"), _pcell("رائیڈر", color="#FFFFFF"), _pcell("کسٹمر", color="#FFFFFF"), _pcell("وقت", color="#FFFFFF")]]
    total = 0
    for r in rows:
        data.append([
            _pcell(r["status"]), _pcell(f"Rs {r['total_amount']:.0f}"), _pcell(r.get("items_summary") or "—"),
            _pcell(r.get("rider_name") or "—"), _pcell(r.get("customer_name") or "—"), _pcell(format_ts(r["timestamp"]), font_size=8)
        ])
        if r["status"] != "missed":
            total += r["total_amount"]
    table = Table(data, colWidths=[18 * mm, 20 * mm, 40 * mm, 25 * mm, 30 * mm, 38 * mm], repeatRows=1)
    table.setStyle(TableStyle([
        ("BACKGROUND", (0, 0), (-1, 0), colors.HexColor((shop.get("primary_color") if shop else None) or "#3B6EA5")),
        ("GRID", (0, 0), (-1, -1), 0.5, colors.grey),
        ("ALIGN", (0, 0), (-1, -1), "RIGHT"),
        ("VALIGN", (0, 0), (-1, -1), "MIDDLE"),
        ("TOPPADDING", (0, 0), (-1, -1), 5),
        ("BOTTOMPADDING", (0, 0), (-1, -1), 5),
        ("ROWBACKGROUNDS", (0, 1), (-1, -1), [colors.white, colors.HexColor("#F8F9FB")]),
    ]))
    elements.append(table)
    elements.append(Spacer(1, 14))
    bold_style = ParagraphStyle("BoldUr3", fontName=pick_font("کل ڈیلیوریز", heading=True), fontSize=13, leading=22, alignment=2, textColor=colors.HexColor("#1F2A37"))
    elements.append(Paragraph(ur(f"کل ڈیلیوریز: {len(rows)}   |   کل رقم: Rs {total:.0f}"), bold_style))

    doc.build(elements)
    buf.seek(0)
    return buf


def generate_walkin_sales_pdf(shop, rows, period_label):
    """A4 PDF report of walk-in (counter/POS) cash sales for a day or month."""
    buf = io.BytesIO()
    doc = SimpleDocTemplate(buf, pagesize=A4, topMargin=24 * mm, bottomMargin=20 * mm, leftMargin=18 * mm, rightMargin=18 * mm)
    elements = []
    body_font = _pdf_header(elements, shop, f"کاؤنٹر سیل رپورٹ — {period_label}")

    data = [[_pcell("طریقہ", color="#FFFFFF"), _pcell("رقم", color="#FFFFFF"), _pcell("آئٹمز", color="#FFFFFF"), _pcell("وقت", color="#FFFFFF")]]
    total = 0
    for r in rows:
        data.append([_pcell(r["payment_method"]), _pcell(f"Rs {r['total_amount']:.0f}"), _pcell(r.get("items_summary") or "—"), _pcell(format_ts(r["timestamp"]), font_size=8)])
        total += r["total_amount"]
    table = Table(data, colWidths=[20 * mm, 25 * mm, 75 * mm, 34 * mm], repeatRows=1)
    table.setStyle(TableStyle([
        ("BACKGROUND", (0, 0), (-1, 0), colors.HexColor((shop.get("primary_color") if shop else None) or "#3B6EA5")),
        ("GRID", (0, 0), (-1, -1), 0.5, colors.grey),
        ("ALIGN", (0, 0), (-1, -1), "RIGHT"),
        ("VALIGN", (0, 0), (-1, -1), "MIDDLE"),
        ("TOPPADDING", (0, 0), (-1, -1), 5),
        ("BOTTOMPADDING", (0, 0), (-1, -1), 5),
        ("ROWBACKGROUNDS", (0, 1), (-1, -1), [colors.white, colors.HexColor("#F8F9FB")]),
    ]))
    elements.append(table)
    elements.append(Spacer(1, 14))
    bold_style = ParagraphStyle("BoldUrW", fontName=pick_font("کل سیلز", heading=True), fontSize=13, leading=22, alignment=2, textColor=colors.HexColor("#1F2A37"))
    elements.append(Paragraph(ur(f"کل سیلز: {len(rows)}   |   کل رقم: Rs {total:.0f}"), bold_style))

    doc.build(elements)
    buf.seek(0)
    return buf


# ----------------------------- THERMAL (POS) RECEIPT -----------------------------

def generate_qr_base64(data, box_size=3):
    if not (QR_AVAILABLE and data):
        return None
    img = qrcode.make(str(data), box_size=box_size, border=2)
    buf = io.BytesIO()
    img.save(buf, format="PNG")
    return base64.b64encode(buf.getvalue()).decode()


def render_thermal_receipt(shop, title, item_rows, total_amount, ref_label, paper_width_mm=58, extra_note=None):
    """Renders a browser-printable thermal receipt (58mm/80mm) with a
    'Print' button that calls window.print(). Most USB/Bluetooth POS
    thermal printers install as a normal OS print driver, so the browser's
    native print dialog picks them up — this is NOT raw ESC/POS printing
    (that would need Web Bluetooth + printer-specific command bytes)."""
    logo_text = (shop.get("logo_text") if shop else None) or "Doodh Delivery System"
    proprietor = (shop.get("proprietor_name") if shop else None) or ""
    primary = (shop.get("primary_color") if shop else None) or "#3B6EA5"

    qr_b64 = generate_qr_base64(ref_label, box_size=3)
    qr_html = f'<div class="qr"><img src="data:image/png;base64,{qr_b64}" width="90" /></div>' if qr_b64 else ""
    proprietor_html = f'<div class="center">{proprietor}</div>' if proprietor else ""
    rows_html = "".join(
        f'<tr><td class="item">{r["name"]}</td><td class="qty">{r["qty"]}</td><td class="amt">{r["amount"]:.0f}</td></tr>'
        for r in item_rows
    )
    now_label = format_ts(datetime.now().isoformat())
    footer_note = extra_note or "شکریہ! دوبارہ تشریف لائیں۔"

    html = (
        "<html><head><meta charset='utf-8'><style>"
        f"@media print {{ @page {{ size: {paper_width_mm}mm auto; margin: 2mm; }} .no-print {{ display: none; }} }}"
        f"body {{ font-family: 'Noto Nastaliq Urdu','Courier New',monospace; width: {paper_width_mm}mm; margin:0 auto; font-size:12px; color:#000; }}"
        ".center { text-align:center; } .shop-name { font-size:15px; font-weight:bold; }"
        ".line { border-top:1px dashed #000; margin:5px 0; }"
        "table { width:100%; border-collapse:collapse; font-size:11px; } td { padding:2px 0; vertical-align:top; }"
        ".qty, .amt { text-align:right; white-space:nowrap; } .total-row { font-size:13px; font-weight:bold; }"
        ".qr { text-align:center; margin-top:8px; } .footer { text-align:center; margin-top:6px; font-size:10px; }"
        f".print-btn {{ display:block; width:100%; margin:10px 0; padding:10px; background:{primary}; color:#fff; border:none; border-radius:6px; font-size:14px; }}"
        "</style></head><body dir='rtl'>"
        "<div class='no-print'><button class='print-btn' onclick='window.print()'>🖨️ پرنٹ کریں</button></div>"
        f"<div class='center shop-name'>{logo_text}</div>"
        f"{proprietor_html}"
        f"<div class='center'>{title}</div>"
        f"<div class='center'>{now_label}</div>"
        "<div class='line'></div>"
        f"<table><tr><td class='item'><b>آئٹم</b></td><td class='qty'><b>مقدار</b></td><td class='amt'><b>رقم</b></td></tr>{rows_html}</table>"
        "<div class='line'></div>"
        f"<table><tr class='total-row'><td class='item'>کل رقم</td><td class='qty'></td><td class='amt'>Rs {total_amount:.0f}</td></tr></table>"
        f"{qr_html}"
        f"<div class='footer'>{footer_note}<br/>NABA Tech | Mobile: 03151186003</div>"
        "</body></html>"
    )
    components.html(html, height=520, scrolling=True)


# ----------------------------- RIDER PANEL -----------------------------

def rider_panel(user):
    shop_id = user["shop_id"]
    rider = get_rider_by_user(user["id"])
    if not rider:
        st.error("آپ کا رائیڈر پروفائل نہیں ملا۔ ایڈمن سے رابطہ کریں۔")
        return

    st.header("🛵 رائیڈر پینل")
    products = get_products(shop_id)
    milk = get_default_quota_product(shop_id)

    for key, default in [("cart", []), ("selected_customer", None), ("pending_order_ids", [])]:
        if key not in st.session_state:
            st.session_state[key] = default

    st.metric("💵 آپ کے پاس موجود نقدی (Cash in Hand)", f"Rs {rider_cash_in_hand(rider['id']):.0f}")

    tab_deliver, tab_cash, tab_history = st.tabs(
        ["🚀 ڈیلیوری (Scan → Confirm)", "💵 کیش کلیکشن", "📋 آج کی ہسٹری"]
    )

    with tab_deliver:
        customers = get_customers(shop_id)

        st.subheader("1️⃣ QR اسکین کریں")
        if QR_SCAN_AVAILABLE:
            img_file = st.camera_input("QR کوڈ اسکین کریں")
            if img_file is not None:
                img = Image.open(img_file)
                results = qr_decode(img)
                if results:
                    scanned = results[0].data.decode("utf-8")
                    cust = verify_customer_qr(scanned, shop_id)
                    if cust:
                        if not st.session_state.selected_customer or st.session_state.selected_customer["id"] != cust["id"]:
                            st.session_state.selected_customer = cust
                            st.session_state.cart = []
                            st.session_state.pending_order_ids = []
                            if milk and cust["daily_quota_kg"] > 0:
                                st.session_state.cart.append({
                                    "product_id": milk["id"], "product_name": milk["name"],
                                    "unit": milk["unit"], "qty": cust["daily_quota_kg"], "rate": milk["rate"]
                                })
                        st.success(f"کسٹمر لوڈ ہو گیا: {cust['name']}")
                    else:
                        st.error("QR کسی کسٹمر سے میچ نہیں ہوا (invalid token)۔")
                else:
                    st.warning("کوئی QR کوڈ نہیں ملا، دوبارہ کوشش کریں۔")
        else:
            st.caption("(QR اسکین کے لیے requirements.txt میں pyzbar اور Pillow شامل کریں)")

        st.markdown("**یا فہرست سے منتخب کریں (fallback):**")
        if customers:
            names = [f"{c['name']} ({c['code']})" for c in customers]
            idx = st.selectbox("کسٹمر", range(len(names)), format_func=lambda i: names[i])
            if st.button("یہ کسٹمر لوڈ کریں"):
                cust = customers[idx]
                st.session_state.selected_customer = cust
                st.session_state.cart = []
                st.session_state.pending_order_ids = []
                if milk and cust["daily_quota_kg"] > 0:
                    st.session_state.cart.append({
                        "product_id": milk["id"], "product_name": milk["name"],
                        "unit": milk["unit"], "qty": cust["daily_quota_kg"], "rate": milk["rate"]
                    })
                st.rerun()
        else:
            st.warning("کوئی کسٹمر موجود نہیں۔ ایڈمن سے کسٹمر شامل کروائیں۔")

        cust = st.session_state.selected_customer
        if cust:
            st.divider()
            st.subheader("2️⃣ کسٹمر کی تفصیل (Auto Loaded)")
            c1, c2, c3 = st.columns(3)
            c1.metric("نام", cust["name"])
            c2.metric("ایڈریس", cust["address"] or "—")
            c3.metric("ڈیفالٹ دودھ", f"{cust['daily_quota_kg']} kg")
            st.caption(f"بقیہ: Rs {customer_balance(cust['id']):.0f}")

            pending_orders = get_extra_orders(shop_id, customer_id=cust["id"], status="pending")
            if pending_orders:
                st.warning(f"🛒 اس کسٹمر کے {len(pending_orders)} پینڈنگ اضافی آرڈر موجود ہیں")
                for po in pending_orders:
                    col_a, col_b = st.columns([3, 1])
                    col_a.write(f"#{po['id']} — {po['items_summary'] or '—'} — Rs {po['total_amount']:.0f}")
                    if col_b.button("➕ کارٹ میں شامل کریں", key=f"pull_order_{po['id']}"):
                        for it in get_extra_order_items(po["id"]):
                            st.session_state.cart.append({
                                "product_id": it["product_id"], "product_name": it["product_name"],
                                "unit": it["unit"], "qty": it["quantity"], "rate": it["rate"]
                            })
                        st.session_state.pending_order_ids.append(po["id"])
                        st.rerun()

            st.subheader("3️⃣ پروڈکٹس شامل کریں")
            for p in products:
                st.markdown(f"**{p['name']}** — Rs {p['rate']:.0f}/{p['unit']}")
                pcols = st.columns(len(unit_presets(p["unit"])))
                for col, (label, qty) in zip(pcols, unit_presets(p["unit"])):
                    if col.button(f"➕ {label}", key=f"qa_{p['id']}_{label}", use_container_width=True):
                        st.session_state.cart.append({
                            "product_id": p["id"], "product_name": p["name"],
                            "unit": p["unit"], "qty": qty, "rate": p["rate"]
                        })
                        st.rerun()

            if st.session_state.cart:
                st.divider()
                st.markdown("#### موجودہ اندراج")
                for i, it in enumerate(st.session_state.cart):
                    col1, col2 = st.columns([4, 1])
                    col1.write(f"{it['product_name']} — {it['qty']}{it['unit']} (Rs {it['qty']*it['rate']:.0f})")
                    if col2.button("🗑️", key=f"del_{i}"):
                        st.session_state.cart.pop(i)
                        st.rerun()

                total_amount = sum(it["qty"] * it["rate"] for it in st.session_state.cart)
                st.markdown(f"**کل رقم: Rs {total_amount:.0f}**")

                st.subheader("4️⃣ کنفرم کریں")
                if st.button("✅ Confirm Delivery", type="primary", use_container_width=True):
                    txn_id = confirm_delivery(cust["id"], rider["id"], st.session_state.cart, shop_id)
                    for order_id in st.session_state.pending_order_ids:
                        fulfill_extra_order(order_id, txn_id)
                    st.session_state.cart = []
                    st.session_state.selected_customer = None
                    st.session_state.pending_order_ids = []
                    st.success("✅ ڈیلیوری فوری سیو اور سنک ہو گئی — اونر ڈیش بورڈ اور کسٹمر پینل اپڈیٹ ہو گئے۔")
                    st.rerun()
            else:
                st.caption("کوئی پروڈکٹ شامل نہیں — اوپر بٹن دبا کر شامل کریں۔")

            st.divider()
            if st.button("❌ آج ناغہ (Missed) مارک کریں"):
                mark_missed(cust["id"], rider["id"], shop_id)
                st.session_state.selected_customer = None
                st.session_state.cart = []
                st.session_state.pending_order_ids = []
                st.success("ناغہ درج کر دیا گیا اور کسٹمر/اونر کو مطلع کر دیا گیا۔")
                st.rerun()
        else:
            st.info("پہلے QR اسکین کریں یا فہرست سے کسٹمر منتخب کریں۔")

    with tab_cash:
        st.subheader("💵 کسٹمر سے نقد وصولی درج کریں")
        st.caption(f"موجودہ نقدی آپ کے پاس: Rs {rider_cash_in_hand(rider['id']):.0f}")
        customers = get_customers(shop_id)
        if customers:
            names = [f"{c['name']} (بقیہ Rs {customer_balance(c['id']):.0f})" for c in customers]
            idx = st.selectbox("کسٹمر", range(len(names)), format_func=lambda i: names[i], key="cash_cust_select")
            amt = st.number_input("وصول شدہ رقم", min_value=0.0, step=50.0, key="cash_amt")
            note = st.text_input("نوٹ (اختیاری)", key="cash_note")
            if st.button("💰 نقد وصولی درج کریں", type="primary"):
                if amt > 0:
                    record_cash_collection(rider["id"], customers[idx]["id"], amt, shop_id, note)
                    st.success(f"Rs {amt:.0f} نقد وصولی درج ہو گئی — کسٹمر کا کھاتہ اپڈیٹ ہو گیا اور آپ کی نقدی میں شامل ہو گئی۔")
                    st.rerun()
                else:
                    st.error("رقم درج کریں۔")
        else:
            st.caption("کوئی کسٹمر موجود نہیں۔")

        st.divider()
        st.subheader("آج کی کیش کلیکشن ہسٹری")
        today = date.today().isoformat()
        conn = get_conn()
        crows = conn.execute(
            "SELECT cc.*, c.name AS customer_name FROM cash_collections cc "
            "LEFT JOIN customers c ON c.id=cc.customer_id "
            "WHERE cc.rider_id=? AND cc.timestamp LIKE ? ORDER BY cc.timestamp DESC",
            (rider["id"], f"{today}%")
        ).fetchall()
        conn.close()
        if crows:
            crows = [dict(r) for r in crows]
            for r in crows:
                r["timestamp"] = format_ts(r["timestamp"])
            dfc = pd.DataFrame(crows)[["timestamp", "customer_name", "amount", "note"]]
            dfc.columns = ["وقت", "کسٹمر", "رقم", "نوٹ"]
            st.dataframe(dfc, use_container_width=True, hide_index=True)
        else:
            st.caption("آج ابھی تک کوئی نقد وصولی درج نہیں ہوئی۔")

    with tab_history:
        today = date.today().isoformat()
        rows = get_transactions(shop_id, rider_id=rider["id"], date_filter=today)
        if rows:
            for r in rows:
                r["timestamp"] = format_ts(r["timestamp"])
            df = pd.DataFrame(rows)[["customer_name", "items_summary", "total_amount", "status", "timestamp"]]
            df.columns = ["کسٹمر", "پروڈکٹس", "رقم", "اسٹیٹس", "وقت"]
            st.dataframe(df, use_container_width=True, hide_index=True)
        else:
            st.caption("آج ابھی تک کوئی ڈیلیوری درج نہیں ہوئی۔")


# ----------------------------- ADMIN PANEL -----------------------------

def admin_panel(user):
    shop_id = user["shop_id"]
    shop = get_shop(shop_id)
    st.header("🧑‍💼 اونر / ایڈمن ڈیش بورڈ")

    if shop:
        badge = {"trial": "🟡 ٹرائل", "active": "🟢 فعال", "expired": "🔴 ختم شدہ", "suspended": "⛔ معطل"}.get(shop["status"], shop["status"])
        expiry = shop.get("license_expires_at") or shop.get("trial_end_date")
        st.caption(f"{badge} — {shop['name']}" + (f" — میعاد: {format_ts(expiry)}" if expiry else ""))

    tabs = st.tabs([
        "📡 لائیو ٹریکنگ", "🛒 اضافی آرڈرز", "🧀 پروڈکٹس / ریٹس", "👥 کسٹمرز", "🛵 رائیڈرز",
        "📒 کھاتہ / لیجر", "💵 وصولی درج کریں", "🧾 کیش سیٹلمنٹ", "🔑 پاسورڈز", "🎨 برانڈنگ",
        "🧾 کاؤنٹر سیل (POS)", "🐄 باڑا / فارم"
    ])

    # ---- Live tracking + admin notifications ----
    with tabs[0]:
        col_h, col_r = st.columns([4, 1])
        col_h.subheader("آج کی لائیو ڈیلیوریز")
        if col_r.button("🔄 ریفریش"):
            st.rerun()

        today = date.today().isoformat()
        rows = get_transactions(shop_id, date_filter=today)
        if rows:
            display_rows = []
            for r in rows:
                display_rows.append({
                    "وقت": format_ts(r["timestamp"]),
                    "رائیڈر": r["rider_name"],
                    "کسٹمر": r["customer_name"],
                    "پروڈکٹس": r["items_summary"] or "—",
                    "رقم": r["total_amount"],
                    "اسٹیٹس": r["status"],
                })
            st.dataframe(pd.DataFrame(display_rows), use_container_width=True, hide_index=True)
            st.metric("آج کل رقم", f"Rs {sum(r['total_amount'] for r in rows):.0f}")
            if PDF_AVAILABLE:
                summary_buf = generate_delivery_summary_pdf(shop, rows, today)
                st.download_button("📄 آج کی سمری PDF ڈاؤن لوڈ کریں", data=summary_buf, file_name=f"delivery_summary_{today}.pdf", mime="application/pdf")
        else:
            st.caption("آج ابھی تک کوئی ڈیلیوری نہیں ہوئی۔")

        if PDF_AVAILABLE:
            st.divider()
            st.subheader("📄 ماہانہ سمری (تمام کسٹمرز)")
            month_for_summary = st.text_input("مہینہ (YYYY-MM)", value=date.today().strftime("%Y-%m"), key="live_month_summary")
            month_rows = get_transactions(shop_id, month_filter=month_for_summary)
            if month_rows:
                monthly_all_buf = generate_delivery_summary_pdf(shop, month_rows, month_for_summary)
                st.download_button("📄 ماہانہ سمری PDF ڈاؤن لوڈ کریں", data=monthly_all_buf, file_name=f"delivery_summary_{month_for_summary}.pdf", mime="application/pdf", key="monthly_all_dl")
            else:
                st.caption("اس مہینے کوئی ریکارڈ نہیں۔")

        st.divider()
        st.subheader("🔔 حالیہ نوٹیفکیشنز")
        notifs = get_notifications("admin", shop_id)
        if notifs:
            for n in notifs:
                st.caption(f"[{format_ts(n['created_at'])}] {n.get('customer_name') or '—'}: {n['message']}")
        else:
            st.caption("کوئی نوٹیفکیشن نہیں۔")

    # ---- Extra (ad-hoc) orders from customers ----
    with tabs[1]:
        col_h2, col_r2 = st.columns([4, 1])
        col_h2.subheader("🛒 کسٹمرز کے اضافی آرڈرز")
        if col_r2.button("🔄 ریفریش", key="refresh_extra_orders"):
            st.rerun()

        pending_orders = get_extra_orders(shop_id, status="pending")
        if pending_orders:
            display_rows = [{
                "وقت": format_ts(o["timestamp"]),
                "کسٹمر": o["customer_name"],
                "آئٹمز": o["items_summary"] or "—",
                "رقم": o["total_amount"],
            } for o in pending_orders]
            st.dataframe(pd.DataFrame(display_rows), use_container_width=True, hide_index=True)
            st.caption("یہ آرڈرز خودکار طور پر رائیڈر کو نظر آئیں گے جب وہ اس کسٹمر کو اسکین/منتخب کرے گا۔")
        else:
            st.caption("فی الحال کوئی پینڈنگ اضافی آرڈر نہیں۔")

        st.divider()
        st.subheader("مکمل شدہ / پرانے آرڈرز")
        all_orders = get_extra_orders(shop_id)
        fulfilled = [o for o in all_orders if o["status"] != "pending"]
        if fulfilled:
            display_rows = [{
                "وقت": format_ts(o["timestamp"]),
                "کسٹمر": o["customer_name"],
                "آئٹمز": o["items_summary"] or "—",
                "رقم": o["total_amount"],
                "اسٹیٹس": o["status"],
            } for o in fulfilled[:30]]
            st.dataframe(pd.DataFrame(display_rows), use_container_width=True, hide_index=True)
        else:
            st.caption("ابھی کوئی مکمل شدہ آرڈر نہیں۔")

    # ---- Products / Rates ----
    with tabs[2]:
        st.subheader("نیا پروڈکٹ شامل کریں")
        with st.form("add_product"):
            p_name = st.text_input("پروڈکٹ کا نام (مثلاً پینیر، کھویا)")
            p_unit = st.selectbox("یونٹ", ["kg", "liter", "packet", "piece", "dozen"])
            p_rate = st.number_input("ریٹ (Rs فی یونٹ)", min_value=0.0, value=100.0, step=10.0)
            submitted = st.form_submit_button("شامل کریں")
            if submitted:
                if not p_name:
                    st.error("پروڈکٹ کا نام درج کریں۔")
                else:
                    add_product(shop_id, p_name, p_unit, p_rate)
                    st.success(f"پروڈکٹ '{p_name}' شامل ہو گیا۔")
                    st.rerun()

        st.divider()
        st.subheader("موجودہ پروڈکٹس")
        all_products = get_products(shop_id, active_only=False)
        for p in all_products:
            with st.expander(f"{'⭐ ' if p['is_default_quota_item'] else ''}{p['name']} — Rs {p['rate']:.0f}/{p['unit']}"):
                with st.form(f"edit_product_{p['id']}"):
                    e_name = st.text_input("نام", value=p["name"], key=f"pname_{p['id']}")
                    unit_opts = ["kg", "liter", "packet", "piece", "dozen"]
                    e_unit = st.selectbox("یونٹ", unit_opts, index=unit_opts.index(p["unit"]) if p["unit"] in unit_opts else 0, key=f"punit_{p['id']}")
                    e_rate = st.number_input("ریٹ", min_value=0.0, value=float(p["rate"]), step=10.0, key=f"prate_{p['id']}")
                    e_active = st.checkbox("فعال (Active)", value=bool(p["active"]), key=f"pactive_{p['id']}")
                    if st.form_submit_button("محفوظ کریں"):
                        update_product(p["id"], shop_id, e_name, e_unit, e_rate, e_active)
                        st.success("اپڈیٹ ہو گیا — رائیڈر اور کسٹمر پینل پر فوراً اثر ہوگا۔")
                        st.rerun()

    # ---- Customers ----
    with tabs[3]:
        st.subheader("نیا کسٹمر شامل کریں")
        with st.form("add_customer"):
            c_name = st.text_input("نام")
            c_address = st.text_input("پتہ")
            c_phone = st.text_input("فون")
            c_code = st.text_input("یونیک کوڈ (اندرونی شناخت)")
            c_quota = st.number_input("روزانہ ڈیفالٹ دودھ (kg)", min_value=0.0, value=1.0, step=0.25)
            make_login = st.checkbox("کسٹمر کے لیے لاگ ان اکاؤنٹ بھی بنائیں", value=True)
            c_user = st.text_input("یوزرنیم (اگر لاگ ان بنانا ہے)")
            c_pass = st.text_input("پاسورڈ (اگر لاگ ان بنانا ہے)", type="password")
            submitted = st.form_submit_button("شامل کریں")
            if submitted:
                if not c_name or not c_code:
                    st.error("نام اور کوڈ ضروری ہیں۔")
                else:
                    conn = get_conn()
                    try:
                        user_id = None
                        if make_login and c_user and c_pass:
                            cur = conn.execute(
                                "INSERT INTO users (username,password,role,name,phone,shop_id) VALUES (?,?,?,?,?,?)",
                                (c_user, hash_pw(c_pass), "customer", c_name, c_phone, shop_id)
                            )
                            user_id = cur.lastrowid
                        cur2 = conn.execute(
                            "INSERT INTO customers (user_id,name,address,phone,code,daily_quota_kg,shop_id) VALUES (?,?,?,?,?,?,?)",
                            (user_id, c_name, c_address, c_phone, c_code, c_quota, shop_id)
                        )
                        new_id = cur2.lastrowid
                        conn.commit()
                        generate_customer_qr_token(new_id)
                        st.success(f"کسٹمر '{c_name}' شامل ہو گیا اور QR جنریٹ ہو گیا۔")
                    except sqlite3.IntegrityError as e:
                        st.error(f"خرابی: یہ کوڈ یا یوزرنیم پہلے سے موجود ہے۔ ({e})")
                    finally:
                        conn.close()

        st.divider()
        st.subheader("موجودہ کسٹمرز")
        customers = get_customers(shop_id)
        if customers:
            for c in customers:
                col1, col2, col3 = st.columns([3, 2, 2])
                col1.write(f"**{c['name']}** — {c['address'] or ''}")
                col2.write(f"کوڈ: `{c['code']}`")
                col3.write(f"بقیہ: Rs {customer_balance(c['id']):.0f}")

                with st.expander(f"QR کوڈ — {c['name']}"):
                    token = c.get("qr_token")
                    if not token and CRYPTO_AVAILABLE:
                        token = generate_customer_qr_token(c["id"])
                    if QR_AVAILABLE and token:
                        qr_img = qrcode.make(token)
                        buf = io.BytesIO()
                        qr_img.save(buf, format="PNG")
                        st.image(buf.getvalue(), width=150, caption="Encrypted QR (customer_id sealed)")
                    if st.button("🔄 QR دوبارہ جنریٹ کریں (پرانا invalid ہو جائے گا)", key=f"regen_{c['id']}"):
                        generate_customer_qr_token(c["id"])
                        st.rerun()
        else:
            st.caption("ابھی کوئی کسٹمر شامل نہیں کیا گیا۔")

    # ---- Riders ----
    with tabs[4]:
        st.subheader("نیا رائیڈر شامل کریں")
        with st.form("add_rider"):
            r_name = st.text_input("نام", key="r_name")
            r_phone = st.text_input("فون", key="r_phone")
            r_user = st.text_input("یوزرنیم")
            r_pass = st.text_input("پاسورڈ", type="password")
            submitted = st.form_submit_button("شامل کریں")
            if submitted:
                if not (r_name and r_user and r_pass):
                    st.error("تمام فیلڈز ضروری ہیں۔")
                else:
                    conn = get_conn()
                    try:
                        cur = conn.execute(
                            "INSERT INTO users (username,password,role,name,phone,shop_id) VALUES (?,?,?,?,?,?)",
                            (r_user, hash_pw(r_pass), "rider", r_name, r_phone, shop_id)
                        )
                        conn.execute(
                            "INSERT INTO riders (user_id,name,phone,shop_id) VALUES (?,?,?,?)",
                            (cur.lastrowid, r_name, r_phone, shop_id)
                        )
                        conn.commit()
                        st.success(f"رائیڈر '{r_name}' شامل ہو گیا۔")
                    except sqlite3.IntegrityError:
                        st.error("یہ یوزرنیم پہلے سے موجود ہے۔")
                    finally:
                        conn.close()

        st.divider()
        conn = get_conn()
        riders = conn.execute("SELECT * FROM riders WHERE active=1 AND shop_id=?", (shop_id,)).fetchall()
        conn.close()
        if riders:
            st.dataframe(pd.DataFrame([dict(r) for r in riders])[["name", "phone"]], hide_index=True, use_container_width=True)

    # ---- Ledger ----
    with tabs[5]:
        st.subheader("ماہانہ کھاتہ / لیجر")
        customers = get_customers(shop_id)
        if customers:
            names = [c["name"] for c in customers]
            sel = st.selectbox("کسٹمر منتخب کریں", range(len(names)), format_func=lambda i: names[i])
            cust = customers[sel]

            month = st.text_input("مہینہ (YYYY-MM)", value=date.today().strftime("%Y-%m"))
            rows = get_transactions(shop_id, customer_id=cust["id"], month_filter=month)
            conn = get_conn()
            pay_rows = conn.execute(
                "SELECT * FROM payments WHERE customer_id=? AND payment_date LIKE ? ORDER BY payment_date",
                (cust["id"], f"{month}%")
            ).fetchall()
            conn.close()

            delivered = [r for r in rows if r["status"] != "missed"]
            missed = [r for r in rows if r["status"] == "missed"]
            total_bill = sum(r["total_amount"] for r in delivered)
            total_paid = sum(r["amount"] for r in pay_rows)

            m1, m2, m3 = st.columns(3)
            m1.metric("کل بل", f"Rs {total_bill:.0f}")
            m2.metric("وصول شدہ", f"Rs {total_paid:.0f}")
            m3.metric("باقی بقیہ", f"Rs {customer_balance(cust['id']):.0f}")
            st.caption(f"ناغے: {len(missed)} دن")

            if rows:
                st.markdown("**تفصیلی ریکارڈ**")
                display_rows = [{
                    "تاریخ/وقت": format_ts(r["timestamp"]),
                    "پروڈکٹس": r["items_summary"] or "—",
                    "رقم": r["total_amount"],
                    "اسٹیٹس": r["status"],
                } for r in rows]
                st.dataframe(pd.DataFrame(display_rows), use_container_width=True, hide_index=True)

                if PDF_AVAILABLE:
                    col_p1, col_p2 = st.columns(2)
                    invoice_buf = generate_invoice_pdf(shop, cust, month, rows, total_bill, total_paid, customer_balance(cust["id"]))
                    col_p1.download_button("📄 اس کسٹمر کا انوائس PDF", data=invoice_buf, file_name=f"invoice_{cust['name']}_{month}.pdf", mime="application/pdf", key="ledger_invoice_dl")
                    monthly_summary_buf = generate_delivery_summary_pdf(shop, rows, month)
                    col_p2.download_button("📄 اس کسٹمر کی ماہانہ سمری PDF", data=monthly_summary_buf, file_name=f"summary_{cust['name']}_{month}.pdf", mime="application/pdf", key="ledger_summary_dl")
        else:
            st.caption("پہلے کسٹمر شامل کریں۔")

    # ---- Record payment ----
    with tabs[6]:
        st.subheader("وصولی درج کریں")
        customers = get_customers(shop_id)
        if customers:
            names = [c["name"] for c in customers]
            sel = st.selectbox("کسٹمر", range(len(names)), format_func=lambda i: names[i], key="pay_cust")
            cust = customers[sel]
            st.caption(f"موجودہ بقیہ: Rs {customer_balance(cust['id']):.0f}")
            with st.form("record_payment"):
                amt = st.number_input("وصول شدہ رقم", min_value=0.0, step=50.0)
                method = st.selectbox("طریقہ", ["cash", "online"])
                note = st.text_input("نوٹ (اختیاری)")
                submitted = st.form_submit_button("درج کریں")
                if submitted and amt > 0:
                    conn = get_conn()
                    conn.execute(
                        "INSERT INTO payments (customer_id,payment_date,amount,method,note,timestamp) VALUES (?,?,?,?,?,?)",
                        (cust["id"], date.today().isoformat(), amt, method, note, datetime.now().isoformat())
                    )
                    conn.commit()
                    conn.close()
                    push_notification(cust["id"], f"آپ کی Rs {amt:.0f} وصولی درج ہو گئی ({method})", shop_id, audience="customer")
                    st.success("وصولی درج ہو گئی۔")
                    st.rerun()
        else:
            st.caption("پہلے کسٹمر شامل کریں۔")

    # ---- Cash Settlement / Recovery ----
    with tabs[7]:
        st.subheader("🧾 رائیڈرز کی نقدی (Cash in Hand)")
        conn = get_conn()
        riders = [dict(r) for r in conn.execute("SELECT * FROM riders WHERE active=1 AND shop_id=?", (shop_id,)).fetchall()]
        conn.close()

        if riders:
            rows_display = [{"رائیڈر": r["name"], "موجودہ نقدی (Rs)": rider_cash_in_hand(r["id"])} for r in riders]
            st.dataframe(pd.DataFrame(rows_display), use_container_width=True, hide_index=True)

            st.divider()
            st.subheader("💰 نقدی وصول کریں (Settlement)")
            names = [r["name"] for r in riders]
            sel = st.selectbox("رائیڈر منتخب کریں", range(len(names)), format_func=lambda i: names[i], key="settle_rider")
            rider = riders[sel]
            in_hand = rider_cash_in_hand(rider["id"])
            st.caption(f"{rider['name']} کے پاس موجود نقدی: Rs {in_hand:.0f}")

            with st.form("settle_cash_form"):
                settle_amt = st.number_input("وصول کی جانے والی رقم", min_value=0.0, value=float(max(in_hand, 0.0)), step=50.0)
                settle_note = st.text_input("نوٹ (اختیاری)")
                submitted = st.form_submit_button("✅ وصول کریں اور کیش ان ہینڈ اپڈیٹ کریں")
                if submitted:
                    if settle_amt <= 0:
                        st.error("رقم درج کریں۔")
                    elif settle_amt > in_hand:
                        st.error(f"یہ رقم رائیڈر کے پاس موجود نقدی (Rs {in_hand:.0f}) سے زیادہ ہے۔")
                    else:
                        settle_rider_cash(rider["id"], settle_amt, settle_note)
                        st.success(f"Rs {settle_amt:.0f} وصول کر لی گئی۔ {rider['name']} کی باقی نقدی: Rs {in_hand - settle_amt:.0f}")
                        st.rerun()

            st.divider()
            st.subheader("سیٹلمنٹ ہسٹری")
            conn = get_conn()
            hist = conn.execute(
                "SELECT cs.*, r.name AS rider_name FROM cash_settlements cs "
                "JOIN riders r ON r.id=cs.rider_id WHERE r.shop_id=? ORDER BY cs.timestamp DESC LIMIT 50",
                (shop_id,)
            ).fetchall()
            conn.close()
            if hist:
                hist = [dict(r) for r in hist]
                for h in hist:
                    h["timestamp"] = format_ts(h["timestamp"])
                dfh = pd.DataFrame(hist)[["timestamp", "rider_name", "amount", "note"]]
                dfh.columns = ["وقت", "رائیڈر", "رقم", "نوٹ"]
                st.dataframe(dfh, use_container_width=True, hide_index=True)
            else:
                st.caption("ابھی تک کوئی سیٹلمنٹ نہیں ہوئی۔")
        else:
            st.caption("پہلے رائیڈر شامل کریں۔")

    # ---- Password reset ----
    with tabs[8]:
        st.subheader("🔑 کسی بھی یوزر کا پاسورڈ ری سیٹ کریں")
        conn = get_conn()
        login_users = conn.execute(
            "SELECT * FROM users WHERE role IN ('rider','customer') AND active=1 AND shop_id=? ORDER BY role, name",
            (shop_id,)
        ).fetchall()
        conn.close()
        login_users = [dict(u) for u in login_users]

        if login_users:
            labels = [f"{u['name']} — {u['role'].upper()} (@{u['username']})" for u in login_users]
            sel = st.selectbox("یوزر منتخب کریں", range(len(labels)), format_func=lambda i: labels[i])
            target = login_users[sel]

            mode = st.radio("نیا پاسورڈ", ["خود لکھیں", "رینڈم جنریٹ کریں"], horizontal=True)
            new_pw = None
            confirm_ok = True

            if mode == "خود لکھیں":
                new_pw = st.text_input("نیا پاسورڈ (کم از کم 4 حروف)", value="123456")
                confirm_pw = st.text_input("پاسورڈ دوبارہ لکھیں (تصدیق)", value="123456")
                if new_pw != confirm_pw:
                    st.warning("دونوں پاسورڈ ایک جیسے نہیں ہیں۔")
                    confirm_ok = False
                elif len(new_pw) < 4:
                    st.warning("پاسورڈ کم از کم 4 حروف کا ہونا چاہیے۔")
                    confirm_ok = False
            else:
                if st.button("🎲 رینڈم پاسورڈ بنائیں"):
                    st.session_state["_gen_pw"] = random_password()
                new_pw = st.session_state.get("_gen_pw")
                if new_pw:
                    st.text_input("جنریٹ شدہ پاسورڈ (کاپی کے لیے)", value=new_pw, disabled=True, key="_gen_pw_display")

            if st.button("✅ پاسورڈ ری سیٹ کریں", type="primary", disabled=not confirm_ok):
                if not new_pw:
                    st.error("پہلے نیا پاسورڈ لکھیں یا جنریٹ کریں۔")
                else:
                    reset_user_password(target["id"], new_pw)
                    st.success(f"{target['name']} (@{target['username']}) کا پاسورڈ ری سیٹ ہو گیا۔ نیا پاسورڈ یوزر کو بتا دیں:")
                    st.code(new_pw, language=None)
                    st.session_state.pop("_gen_pw", None)
        else:
            st.caption("ابھی کوئی رائیڈر/کسٹمر لاگ ان اکاؤنٹ موجود نہیں۔")

    # ---- Branding (shop admin can tweak within their own shop) ----
    with tabs[9]:
        st.subheader("🎨 برانڈنگ")
        st.caption("یہ صرف آپ کی اپنی شاپ پر لاگو ہوں گے۔")
        if shop:
            st.markdown("#### ٹیکسٹ اور لوگو")
            new_logo_text = st.text_input("کمپنی کا نام / لوگو ٹیکسٹ (پہلی لائن)", value=shop.get("logo_text") or "Doodh Delivery System")
            new_proprietor = st.text_input("پروپرائیٹر کا نام (دوسری لائن، اختیاری)", value=shop.get("proprietor_name") or "")
            st.caption("تیسری لائن ہمیشہ ہلکی اور چھوٹی نظر آئے گی: \"NABA Tech | Mobile: 03151186003\" (یہ فکسڈ ہے، تبدیل نہیں ہوتی)۔")

            st.markdown("#### لوگو")
            current_logo = shop.get("logo_image_base64")
            if current_logo:
                st.image(base64.b64decode(current_logo), width=80, caption="موجودہ لوگو")
                remove_logo = st.checkbox("لوگو ہٹا کر دوبارہ ایموجی استعمال کریں")
            else:
                remove_logo = False
            new_logo_emoji = st.text_input("لوگو ایموجی (اگر PNG اپلوڈ نہ کریں تو یہ استعمال ہوگا)", value=shop.get("logo_emoji") or "🥛")
            uploaded_logo = st.file_uploader("لوگو PNG اپلوڈ کریں (اختیاری)", type=["png"])

            st.markdown("#### تھیم کے رنگ")
            col1, col2 = st.columns(2)
            new_primary = col1.color_picker("پرائمری رنگ (بٹنز/بینر)", value=shop.get("primary_color") or "#3B6EA5")
            new_accent = col2.color_picker("ایکسنٹ رنگ (بینر گریڈینٹ)", value=shop.get("accent_color") or "#5B8FC4")
            new_bg = col1.color_picker("بیک گراؤنڈ رنگ", value=shop.get("background_color") or "#FFFFFF")
            new_text = col2.color_picker("ٹیکسٹ رنگ", value=shop.get("text_color") or "#1F2A37")
            new_secondary = col1.color_picker("سیکنڈری رنگ (کارڈز/سائیڈ بار)", value=shop.get("secondary_color") or "#F1F3F5")

            if st.button("✅ برانڈنگ محفوظ کریں", type="primary"):
                conn = get_conn()
                logo_b64 = current_logo
                if uploaded_logo is not None:
                    logo_b64 = base64.b64encode(uploaded_logo.getvalue()).decode()
                elif remove_logo:
                    logo_b64 = None
                conn.execute(
                    "UPDATE shops SET primary_color=?, logo_emoji=?, logo_text=?, proprietor_name=?, "
                    "logo_image_base64=?, background_color=?, text_color=?, secondary_color=?, accent_color=? WHERE id=?",
                    (new_primary, new_logo_emoji, new_logo_text, new_proprietor,
                     logo_b64, new_bg, new_text, new_secondary, new_accent, shop_id)
                )
                conn.commit()
                conn.close()
                st.success("برانڈنگ محفوظ ہو گئی۔")
                st.rerun()

    # ---- Walk-in Customer / POS ----
    with tabs[10]:
        st.subheader("🧾 کاؤنٹر / واکنگ کسٹمر — فوری نقد سیل")
        if "pos_cart" not in st.session_state:
            st.session_state.pos_cart = []

        pos_products = get_products(shop_id)
        for p in pos_products:
            pcols = st.columns(len(unit_presets(p["unit"])) + 1)
            pcols[0].write(f"**{p['name']}** — Rs {p['rate']:.0f}/{p['unit']}")
            for col, (label, qty) in zip(pcols[1:], unit_presets(p["unit"])):
                if col.button(f"➕ {label}", key=f"pos_qa_{p['id']}_{label}", use_container_width=True):
                    st.session_state.pos_cart.append({
                        "product_id": p["id"], "product_name": p["name"], "unit": p["unit"], "qty": qty, "rate": p["rate"]
                    })
                    st.rerun()

        if st.session_state.pos_cart:
            st.divider()
            st.markdown("#### کارٹ")
            for i, it in enumerate(st.session_state.pos_cart):
                c1, c2 = st.columns([4, 1])
                c1.write(f"{it['product_name']} — {it['qty']}{it['unit']} = Rs {it['qty']*it['rate']:.0f}")
                if c2.button("🗑️", key=f"pos_del_{i}"):
                    st.session_state.pos_cart.pop(i)
                    st.rerun()

            pos_total = sum(it["qty"] * it["rate"] for it in st.session_state.pos_cart)
            st.markdown(f"**کل رقم: Rs {pos_total:.0f}**")
            payment_method = st.selectbox("ادائیگی کا طریقہ", ["cash", "online"], key="pos_payment_method")

            if st.button("✅ سیل مکمل کریں", type="primary", use_container_width=True):
                sale_id, amount = record_walk_in_sale(shop_id, st.session_state.pos_cart, payment_method)
                st.session_state.pos_last_sale = sale_id
                st.session_state.pos_cart = []
                st.success(f"سیل #{sale_id} مکمل ہو گئی — Rs {amount:.0f}")
                st.rerun()
        else:
            st.caption("اوپر بٹن دبا کر آئٹمز شامل کریں۔")

        if st.session_state.get("pos_last_sale"):
            st.divider()
            st.subheader("🖨️ آخری سیل کی رسید")
            sale = next((s for s in get_walk_in_sales(shop_id) if s["id"] == st.session_state.pos_last_sale), None)
            if sale:
                items = get_walk_in_sale_items(sale["id"])
                paper = st.radio("پیپر سائز", ["58mm", "80mm"], horizontal=True, key="pos_paper_width")
                width_mm = 58 if paper == "58mm" else 80
                item_rows = [{"name": it["product_name"], "qty": f"{it['quantity']}{it['unit']}", "amount": it["amount"]} for it in items]
                render_thermal_receipt(shop, "کاؤنٹر سیل رسید", item_rows, sale["total_amount"], f"WalkIn-{sale['id']}", paper_width_mm=width_mm)

        st.divider()
        st.subheader("📋 آج کی کاؤنٹر سیلز")
        today = date.today().isoformat()
        today_sales = get_walk_in_sales(shop_id, date_filter=today)
        if today_sales:
            display_rows = [{"وقت": format_ts(s["timestamp"]), "آئٹمز": s["items_summary"] or "—", "رقم": s["total_amount"], "طریقہ": s["payment_method"]} for s in today_sales]
            st.dataframe(pd.DataFrame(display_rows), use_container_width=True, hide_index=True)
            st.metric("آج کاؤنٹر سیلز کل رقم", f"Rs {sum(s['total_amount'] for s in today_sales):.0f}")
            if PDF_AVAILABLE:
                pdf_buf = generate_walkin_sales_pdf(shop, today_sales, today)
                st.download_button("📄 آج کی POS رپورٹ PDF", data=pdf_buf, file_name=f"pos_report_{today}.pdf", mime="application/pdf")
        else:
            st.caption("آج ابھی تک کوئی کاؤنٹر سیل نہیں ہوئی۔")

    # ---- Baara / Farm milk reconciliation ----
    with tabs[11]:
        st.subheader("🐄 باڑے سے آمد درج کریں")
        with st.form("farm_supply_form"):
            supply_date = st.date_input("تاریخ", value=date.today())
            qty_kg = st.number_input("کل دودھ کی مقدار (kg)", min_value=0.0, step=1.0)
            note = st.text_input("نوٹ (اختیاری)")
            if st.form_submit_button("✅ درج کریں", type="primary"):
                if qty_kg > 0:
                    record_farm_supply(shop_id, supply_date.isoformat(), qty_kg, note)
                    st.success("باڑے کی سپلائی درج ہو گئی۔")
                    st.rerun()
                else:
                    st.error("مقدار درج کریں۔")

        st.divider()
        st.subheader("📊 روزانہ حساب کا تقابل (Reconciliation)")
        recon_date = st.date_input("تاریخ منتخب کریں", value=date.today(), key="recon_date")
        recon = get_daily_reconciliation(shop_id, recon_date.isoformat())
        if recon["milk_product"]:
            m1, m2, m3, m4 = st.columns(4)
            m1.metric("باڑے سے آیا", f"{recon['farm_total']:.2f} kg")
            m2.metric("سبسکرپشن میں گیا", f"{recon['subscription_used']:.2f} kg")
            m3.metric("واکنگ کسٹمرز کو بکا", f"{recon['walkin_sold']:.2f} kg")
            m4.metric("باقی بچا", f"{recon['remaining']:.2f} kg")
            if recon["remaining"] < 0:
                st.error("⚠️ خرچ باڑے سے آئے دودھ سے زیادہ ہو گیا ہے — ریکارڈ چیک کریں۔")
        else:
            st.warning("پہلے پروڈکٹس میں دودھ کو ⭐ ڈیفالٹ آئٹم کے طور پر سیٹ کریں۔")

        st.divider()
        st.subheader("باڑے کی حالیہ ہسٹری")
        farm_rows = get_farm_supply(shop_id)
        if farm_rows:
            display_rows = [{"تاریخ": r["supply_date"], "مقدار (kg)": r["quantity_kg"], "نوٹ": r["note"] or "—"} for r in farm_rows[:30]]
            st.dataframe(pd.DataFrame(display_rows), use_container_width=True, hide_index=True)
        else:
            st.caption("ابھی تک کوئی باڑے کی انٹری نہیں ہوئی۔")


# ----------------------------- CUSTOMER PANEL -----------------------------

def customer_panel(user):
    shop_id = user["shop_id"]
    conn = get_conn()
    cust = conn.execute("SELECT * FROM customers WHERE user_id=?", (user["id"],)).fetchone()
    conn.close()
    if not cust:
        st.error("آپ کا کسٹمر پروفائل نہیں ملا۔ ایڈمن سے رابطہ کریں۔")
        return
    cust = dict(cust)

    st.header(f"👤 خوش آمدید، {cust['name']}")

    col_rate, col_qr = st.columns([2, 1])
    with col_rate:
        milk = get_default_quota_product(shop_id)
        if milk:
            st.info(f"آج کا دودھ ریٹ: **Rs {milk['rate']:.0f} / {milk['unit']}**")
        products = get_products(shop_id)
        if products:
            st.caption(" | ".join(f"{p['name']}: Rs {p['rate']:.0f}/{p['unit']}" for p in products if not p["is_default_quota_item"]))

    with col_qr:
        st.markdown("**📱 آپ کا QR کوڈ**")
        token = cust.get("qr_token")
        if not token and CRYPTO_AVAILABLE:
            token = generate_customer_qr_token(cust["id"])
            cust["qr_token"] = token
        if QR_AVAILABLE and token:
            qr_img = qrcode.make(token)
            buf = io.BytesIO()
            qr_img.save(buf, format="PNG")
            st.image(buf.getvalue(), width=150, caption="رائیڈر کو یہ دکھائیں")
        else:
            st.caption(f"آپ کا شناختی کوڈ: `{cust['code']}`")

    st.divider()
    st.subheader("🛒 اضافی سامان کا آرڈر")
    st.caption("دودھ کی باقاعدہ ڈیلیوری کے علاوہ کوئی اضافی چیز چاہیے تو یہاں سے منتخب کریں۔")

    if "extra_cart" not in st.session_state:
        st.session_state.extra_cart = []

    extra_products = [p for p in get_products(shop_id) if not p["is_default_quota_item"]]
    if extra_products:
        for p in extra_products:
            col1, col2, col3 = st.columns([3, 2, 1])
            col1.write(f"**{p['name']}** — Rs {p['rate']:.0f}/{p['unit']}")
            qty = col2.number_input("مقدار", min_value=0.0, value=0.0, step=0.25 if p["unit"] == "kg" else 1.0, key=f"extra_qty_{p['id']}", label_visibility="collapsed")
            if col3.button("➕ شامل کریں", key=f"extra_add_{p['id']}"):
                if qty > 0:
                    st.session_state.extra_cart.append({
                        "product_id": p["id"], "product_name": p["name"],
                        "unit": p["unit"], "qty": qty, "rate": p["rate"]
                    })
                    st.rerun()
                else:
                    st.warning("پہلے مقدار درج کریں۔")

        if st.session_state.extra_cart:
            st.markdown("#### آپ کی موجودہ سلیکشن")
            for i, it in enumerate(st.session_state.extra_cart):
                cc1, cc2 = st.columns([4, 1])
                cc1.write(f"{it['product_name']} — {it['qty']}{it['unit']} = Rs {it['qty']*it['rate']:.0f}")
                if cc2.button("🗑️", key=f"extra_del_{i}"):
                    st.session_state.extra_cart.pop(i)
                    st.rerun()
            running_total = sum(it["qty"] * it["rate"] for it in st.session_state.extra_cart)
            st.markdown(f"**لائیو کل رقم: Rs {running_total:.0f}**")
            if st.button("✅ آرڈر جمع کروائیں", type="primary"):
                place_extra_order(shop_id, cust["id"], st.session_state.extra_cart)
                st.session_state.extra_cart = []
                st.success("آپ کا آرڈر جمع ہو گیا — رائیڈر اگلی ڈیلیوری پر یہ سامان لے آئے گا۔")
                st.rerun()
    else:
        st.caption("فی الحال کوئی اضافی پروڈکٹ دستیاب نہیں۔")

    my_orders = get_extra_orders(shop_id, customer_id=cust["id"])
    if my_orders:
        st.markdown("#### میرے اضافی آرڈرز")
        shop_for_pdf = get_shop(shop_id)
        for o in my_orders[:10]:
            status_label = {"pending": "⏳ پینڈنگ", "fulfilled": "✅ مکمل", "cancelled": "❌ منسوخ"}.get(o["status"], o["status"])
            col1, col2 = st.columns([4, 1])
            col1.write(f"#{o['id']} — {format_ts(o['timestamp'])} — {o['items_summary'] or '—'} — Rs {o['total_amount']:.0f} — {status_label}")
            if PDF_AVAILABLE:
                items = get_extra_order_items(o["id"])
                pdf_buf = generate_extra_order_receipt_pdf(shop_for_pdf, cust, o, items)
                col2.download_button("📄 رسید", data=pdf_buf, file_name=f"receipt_{o['id']}.pdf", mime="application/pdf", key=f"receipt_dl_{o['id']}")

    month = date.today().strftime("%Y-%m")
    rows = get_transactions(shop_id, customer_id=cust["id"], month_filter=month)
    conn = get_conn()
    pay_rows = conn.execute(
        "SELECT * FROM payments WHERE customer_id=? AND payment_date LIKE ? ORDER BY payment_date DESC",
        (cust["id"], f"{month}%")
    ).fetchall()
    conn.close()

    delivered = [r for r in rows if r["status"] != "missed"]
    missed = [r for r in rows if r["status"] == "missed"]
    total_bill = sum(r["total_amount"] for r in delivered)
    total_paid = sum(r["amount"] for r in pay_rows)

    m1, m2, m3 = st.columns(3)
    m1.metric("اس مہینے کل بل", f"Rs {total_bill:.0f}")
    m2.metric("جمع کروائی رقم", f"Rs {total_paid:.0f}")
    m3.metric("باقی بقیہ", f"Rs {customer_balance(cust['id']):.0f}")
    st.caption(f"اس مہینے ناغے: {len(missed)} دن")

    st.subheader("ڈیلیوری تفصیل (اس مہینے)")
    if rows:
        display_rows = [{
            "تاریخ/وقت": format_ts(r["timestamp"]),
            "پروڈکٹس": r["items_summary"] or "—",
            "رقم": r["total_amount"],
            "اسٹیٹس": r["status"],
        } for r in rows]
        st.dataframe(pd.DataFrame(display_rows), use_container_width=True, hide_index=True)
        if PDF_AVAILABLE:
            shop_for_pdf = get_shop(shop_id)
            invoice_buf = generate_invoice_pdf(shop_for_pdf, cust, month, rows, total_bill, total_paid, customer_balance(cust["id"]))
            st.download_button("📄 ماہانہ انوائس PDF ڈاؤن لوڈ کریں", data=invoice_buf, file_name=f"invoice_{cust['name']}_{month}.pdf", mime="application/pdf")
    else:
        st.caption("اس مہینے ابھی تک کوئی ریکارڈ نہیں۔")

    st.subheader("وصولی کی تفصیل")
    if pay_rows:
        pay_rows = [dict(r) for r in pay_rows]
        for r in pay_rows:
            r["timestamp"] = format_ts(r["timestamp"])
        dfp = pd.DataFrame(pay_rows)[["timestamp", "amount", "method", "note"]]
        dfp.columns = ["وقت", "رقم", "طریقہ", "نوٹ"]
        st.dataframe(dfp, use_container_width=True, hide_index=True)
    else:
        st.caption("اس مہینے کوئی وصولی درج نہیں ہوئی۔")

    st.subheader("🔔 نوٹیفکیشنز")
    notifs = get_notifications("customer", shop_id, customer_id=cust["id"])
    if notifs:
        for n in notifs:
            st.caption(f"[{format_ts(n['created_at'])}] {n['message']}")
    else:
        st.caption("کوئی نوٹیفکیشن نہیں۔")


# ----------------------------- MASTER ADMIN PANEL -----------------------------

def master_admin_panel(user):
    st.header("👑 ماسٹر ایڈمن پینل")
    tabs = st.tabs(["🏪 شاپس", "🔑 لائسنس کیز", "📊 مانیٹرنگ", "📢 اعلان (Broadcast)"])

    with tabs[0]:
        st.subheader("نئی شاپ بنائیں")
        with st.form("new_shop"):
            s_name = st.text_input("شاپ کا نام")
            a_name = st.text_input("پہلے ایڈمن کا نام")
            a_user = st.text_input("ایڈمن یوزرنیم")
            a_pass = st.text_input("ایڈمن پاسورڈ", type="password")
            trial_days = st.number_input("ٹرائل دن", min_value=1, value=15)
            if st.form_submit_button("✅ شاپ بنائیں", type="primary"):
                if not (s_name and a_name and a_user and a_pass):
                    st.error("تمام فیلڈز درکار ہیں۔")
                else:
                    try:
                        create_shop(s_name, a_user, a_pass, a_name, trial_days)
                        st.success(f"'{s_name}' بن گئی — {trial_days} دن کا ٹرائل شروع۔ ایڈمن لاگ ان: {a_user}")
                        st.rerun()
                    except sqlite3.IntegrityError:
                        st.error("یہ یوزرنیم پہلے سے موجود ہے۔")

        st.divider()
        st.subheader("موجودہ شاپس")
        conn = get_conn()
        shops = [dict(r) for r in conn.execute("SELECT * FROM shops ORDER BY created_at DESC").fetchall()]
        conn.close()
        for s in shops:
            _, reason = check_shop_access(s["id"])
            with st.expander(f"{s['name']} — {s['status'].upper()}"):
                st.write(f"ٹرائل ختم: {format_ts(s['trial_end_date']) if s['trial_end_date'] else '—'}")
                st.write(f"لائسنس ختم: {format_ts(s['license_expires_at']) if s['license_expires_at'] else '—'}")
                if reason:
                    st.warning(reason)

                colA, colB = st.columns(2)
                if colA.button("⏸️ معطل کریں", key=f"suspend_{s['id']}"):
                    conn = get_conn()
                    conn.execute("UPDATE shops SET status='suspended' WHERE id=?", (s["id"],))
                    conn.commit()
                    conn.close()
                    st.rerun()
                if colB.button("▶️ فعال کریں (1 سال)", key=f"activate_{s['id']}"):
                    conn = get_conn()
                    conn.execute(
                        "UPDATE shops SET status='active', license_expires_at=? WHERE id=?",
                        ((datetime.now() + timedelta(days=365)).isoformat(), s["id"])
                    )
                    conn.commit()
                    conn.close()
                    st.rerun()

        if not shops:
            st.caption("ابھی کوئی شاپ نہیں بنائی گئی۔")

    with tabs[1]:
        st.subheader("لائسنس کی جنریٹ کریں")
        conn = get_conn()
        shops = [dict(r) for r in conn.execute("SELECT * FROM shops ORDER BY name").fetchall()]
        conn.close()
        if shops:
            names = [s["name"] for s in shops]
            sel = st.selectbox("شاپ منتخب کریں", range(len(names)), format_func=lambda i: names[i])
            dur = st.number_input("مدت (دن)", min_value=1, value=365)
            if st.button("🔑 نئی کی جنریٹ کریں", type="primary"):
                key_code = generate_license_key(shops[sel]["id"], dur)
                st.success("نئی activation key بن گئی — کلائنٹ کو یہ دیں:")
                st.code(key_code, language=None)

            st.divider()
            st.subheader("جاری شدہ کیز")
            conn = get_conn()
            keys = [dict(r) for r in conn.execute(
                "SELECT lk.*, s.name AS shop_name FROM license_keys lk JOIN shops s ON s.id=lk.shop_id ORDER BY lk.created_at DESC LIMIT 50"
            ).fetchall()]
            conn.close()
            if keys:
                for k in keys:
                    k["created_at"] = format_ts(k["created_at"])
                    k["used_at"] = format_ts(k["used_at"]) if k["used_at"] else "—"
                    k["is_used"] = "ہاں" if k["is_used"] else "نہیں"
                df = pd.DataFrame(keys)[["shop_name", "key_code", "duration_days", "is_used", "created_at", "used_at"]]
                df.columns = ["شاپ", "کی", "مدت (دن)", "استعمال شدہ", "بنی", "استعمال ہوئی"]
                st.dataframe(df, use_container_width=True, hide_index=True)
        else:
            st.caption("پہلے شاپ بنائیں۔")

    with tabs[2]:
        st.subheader("📊 تمام شاپس کی آج کی ڈیلیوریز")
        today = date.today().isoformat()
        conn = get_conn()
        rows = conn.execute(
            "SELECT dt.*, s.name AS shop_name, c.name AS customer_name, r.name AS rider_name, "
            "GROUP_CONCAT(di.product_name || ' ' || di.quantity || di.unit, ', ') AS items_summary "
            "FROM delivery_txns dt "
            "JOIN shops s ON s.id=dt.shop_id "
            "JOIN customers c ON c.id=dt.customer_id "
            "JOIN riders r ON r.id=dt.rider_id "
            "LEFT JOIN delivery_items di ON di.transaction_id=dt.id "
            "WHERE dt.delivery_date=? GROUP BY dt.id ORDER BY dt.timestamp DESC",
            (today,)
        ).fetchall()
        conn.close()
        if rows:
            rows = [dict(r) for r in rows]
            for r in rows:
                r["timestamp"] = format_ts(r["timestamp"])
            df = pd.DataFrame(rows)[["shop_name", "timestamp", "rider_name", "customer_name", "items_summary", "total_amount", "status"]]
            df.columns = ["شاپ", "وقت", "رائیڈر", "کسٹمر", "پروڈکٹس", "رقم", "اسٹیٹس"]
            st.dataframe(df, use_container_width=True, hide_index=True)
            st.metric("تمام شاپس کی آج کل ڈیلیوریز", len(rows))
        else:
            st.caption("آج ابھی تک کوئی ڈیلیوری نہیں ہوئی۔")

        st.divider()
        st.subheader("فی شاپ خلاصہ")
        conn = get_conn()
        shop_counts = conn.execute(
            "SELECT s.name AS شاپ, "
            "(SELECT COUNT(*) FROM customers WHERE shop_id=s.id) AS کسٹمرز, "
            "(SELECT COUNT(*) FROM riders WHERE shop_id=s.id) AS رائیڈرز, "
            "(SELECT COUNT(*) FROM products WHERE shop_id=s.id) AS پروڈکٹس "
            "FROM shops s"
        ).fetchall()
        conn.close()
        if shop_counts:
            st.dataframe(pd.DataFrame([dict(r) for r in shop_counts]), use_container_width=True, hide_index=True)

    with tabs[3]:
        st.subheader("📢 تمام یوزرز کو اعلان/گریٹنگ بھیجیں")
        st.caption("یہ پیغام ہر شاپ کے ہر یوزر (ایڈمن، رائیڈر، کسٹمر) کو لاگ ان کرتے ہی نظر آئے گا۔")
        msg = st.text_area("پیغام لکھیں", placeholder="مثلاً: عید مبارک! کل ڈیلیوری معمول کے مطابق جاری رہے گی۔")
        if st.button("✅ سب کو بھیجیں", type="primary"):
            if msg.strip():
                post_broadcast(msg.strip())
                st.success("اعلان بھیج دیا گیا — تمام یوزرز کو نظر آئے گا۔")
                st.rerun()
            else:
                st.error("پہلے پیغام لکھیں۔")

        st.divider()
        st.subheader("پرانے اعلانات")
        conn = get_conn()
        history = [dict(r) for r in conn.execute("SELECT * FROM broadcast_messages ORDER BY created_at DESC LIMIT 20").fetchall()]
        conn.close()
        if history:
            for h in history:
                st.caption(f"[{format_ts(h['created_at'])}] {h['message']}")
        else:
            st.caption("ابھی تک کوئی اعلان نہیں بھیجا گیا۔")


# ----------------------------- MAIN -----------------------------

def main():
    st.set_page_config(page_title="Doodh Delivery System", page_icon="🥛", layout="wide")
    init_db()

    if "user" not in st.session_state:
        apply_theme()
        render_banner()
        login_page()
        return

    user = st.session_state.user

    if user["role"] == "master_admin":
        apply_theme()
        render_banner()
        logout_button()
        master_admin_panel(user)
    else:
        shop = get_shop(user["shop_id"])
        apply_theme(shop)
        render_banner(shop)
        logout_button()
        render_broadcasts()

        allowed, _ = check_shop_access(user["shop_id"])
        if not allowed:
            license_lock_screen(user)
        elif user["role"] == "admin":
            admin_panel(user)
        elif user["role"] == "rider":
            rider_panel(user)
        elif user["role"] == "customer":
            customer_panel(user)

    st.markdown("---")
    st.caption("NABA TECH BY KALEEM ULLAH SHARIF")


if __name__ == "__main__":
    main()
