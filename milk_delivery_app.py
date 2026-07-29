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
import sqlite3
from datetime import datetime, date, timedelta
import hashlib
import io
import secrets
import string
import pandas as pd

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
    st.markdown(f"""
    <style>
        @import url('https://fonts.googleapis.com/css2?family=Poppins:wght@400;500;600;700&display=swap');

        html, body, .stApp, [data-testid="stAppViewContainer"], [data-testid="stMain"] {{
            background-color: #FFFFFF !important;
            color: #1F2A37 !important;
            font-family: 'Poppins', sans-serif !important;
        }}
        [data-testid="stHeader"] {{ background-color: #FFFFFF !important; }}
        [data-testid="stSidebar"] {{ background-color: #F1F3F5 !important; }}

        .naba-banner {{
            background: linear-gradient(90deg, {primary}CC 0%, {primary} 60%, {primary}AA 100%);
            padding: 18px 24px;
            border-radius: 14px;
            margin-bottom: 18px;
            box-shadow: 0 4px 14px rgba(0,0,0,0.15);
        }}
        .naba-banner h1 {{ color: #FFFFFF !important; margin: 0; font-size: 26px; font-weight: 700; }}
        .naba-banner p {{ color: #EAF0FA !important; margin: 2px 0 0 0; font-size: 13px; }}

        [data-testid="stMetric"] {{
            background-color: #F8F9FB !important;
            padding: 14px !important;
            border-radius: 14px !important;
            border: 1px solid #E6E9EE !important;
            box-shadow: 0 2px 6px rgba(31,42,55,0.05);
        }}
        [data-testid="stMetricLabel"] {{ color: #6B7280 !important; }}
        [data-testid="stMetricValue"] {{ color: #1F2A37 !important; font-weight: 600; }}
        [data-testid="stExpander"] {{
            border-radius: 12px !important;
            border: 1px solid #E6E9EE !important;
            box-shadow: 0 2px 6px rgba(31,42,55,0.04);
        }}

        h1, h2, h3, h4, h5 {{ color: #1F2A37; font-weight: 600; }}
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
    st.markdown(f"""
    <div class="naba-banner">
        <h1>{emoji} {text}</h1>
        <p>NABA TECH BY KALEEM ULLAH SHARIF</p>
    </div>
    """, unsafe_allow_html=True)


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

    for key, default in [("cart", []), ("selected_customer", None)]:
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
                    confirm_delivery(cust["id"], rider["id"], st.session_state.cart, shop_id)
                    st.session_state.cart = []
                    st.session_state.selected_customer = None
                    st.success("✅ ڈیلیوری فوری سیو اور سنک ہو گئی — اونر ڈیش بورڈ اور کسٹمر پینل اپڈیٹ ہو گئے۔")
                    st.rerun()
            else:
                st.caption("کوئی پروڈکٹ شامل نہیں — اوپر بٹن دبا کر شامل کریں۔")

            st.divider()
            if st.button("❌ آج ناغہ (Missed) مارک کریں"):
                mark_missed(cust["id"], rider["id"], shop_id)
                st.session_state.selected_customer = None
                st.session_state.cart = []
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
        "📡 لائیو ٹریکنگ", "🧀 پروڈکٹس / ریٹس", "👥 کسٹمرز", "🛵 رائیڈرز",
        "📒 کھاتہ / لیجر", "💵 وصولی درج کریں", "🧾 کیش سیٹلمنٹ", "🔑 پاسورڈز", "🎨 برانڈنگ"
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
        else:
            st.caption("آج ابھی تک کوئی ڈیلیوری نہیں ہوئی۔")

        st.divider()
        st.subheader("🔔 حالیہ نوٹیفکیشنز")
        notifs = get_notifications("admin", shop_id)
        if notifs:
            for n in notifs:
                st.caption(f"[{format_ts(n['created_at'])}] {n.get('customer_name') or '—'}: {n['message']}")
        else:
            st.caption("کوئی نوٹیفکیشن نہیں۔")

    # ---- Products / Rates ----
    with tabs[1]:
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
    with tabs[2]:
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
    with tabs[3]:
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
    with tabs[4]:
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
        else:
            st.caption("پہلے کسٹمر شامل کریں۔")

    # ---- Record payment ----
    with tabs[5]:
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
    with tabs[6]:
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
    with tabs[7]:
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
    with tabs[8]:
        st.subheader("🎨 برانڈنگ")
        st.caption("یہ رنگ اور لوگو صرف آپ کی اپنی شاپ پر لاگو ہوں گے۔")
        if shop:
            new_color = st.color_picker("پرائمری رنگ", value=shop.get("primary_color") or "#3B6EA5")
            new_logo_emoji = st.text_input("لوگو ایموجی", value=shop.get("logo_emoji") or "🥛")
            new_logo_text = st.text_input("لوگو ٹیکسٹ", value=shop.get("logo_text") or "Doodh Delivery System")
            if st.button("محفوظ کریں", type="primary"):
                conn = get_conn()
                conn.execute(
                    "UPDATE shops SET primary_color=?, logo_emoji=?, logo_text=? WHERE id=?",
                    (new_color, new_logo_emoji, new_logo_text, shop_id)
                )
                conn.commit()
                conn.close()
                st.success("برانڈنگ محفوظ ہو گئی۔")
                st.rerun()


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
    tabs = st.tabs(["🏪 شاپس", "🔑 لائسنس کیز", "📊 مانیٹرنگ"])

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
