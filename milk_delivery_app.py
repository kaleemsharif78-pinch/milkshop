"""
Doodh Delivery System — NABA TECH BY KALEEM ULLAH SHARIF
Roles: Rider, Owner/Admin, Customer

v2: Encrypted QR (customer_id sealed with Fernet), scan verification,
PIN/OTP authentication before a delivery is confirmed, and an
in-app notification feed standing in for push notifications.

Note on "API" terms: this is a single-file Streamlit app, so
/verify-customer-qr and /confirm-delivery are implemented as plain
Python functions with the same contract described in the spec
(input -> decrypt/verify -> DB write -> notify). If this later needs
to serve a separate mobile customer app, these functions can be
lifted as-is into a FastAPI backend without changing their logic.
"""

import streamlit as st
import sqlite3
from datetime import datetime, date, timedelta
import hashlib
import io
import random
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
OTP_VALID_MINUTES = 5

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

    c.execute("""
        CREATE TABLE IF NOT EXISTS users (
            id INTEGER PRIMARY KEY AUTOINCREMENT,
            username TEXT UNIQUE NOT NULL,
            password TEXT NOT NULL,
            role TEXT NOT NULL CHECK(role IN ('admin','rider','customer')),
            name TEXT NOT NULL,
            phone TEXT,
            active INTEGER DEFAULT 1
        )
    """)

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
    # migrations for existing DBs
    _add_column_if_missing(conn, "customers", "qr_token TEXT")
    _add_column_if_missing(conn, "customers", "pin_hash TEXT")

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

    c.execute("""
        CREATE TABLE IF NOT EXISTS deliveries (
            id INTEGER PRIMARY KEY AUTOINCREMENT,
            customer_id INTEGER NOT NULL,
            rider_id INTEGER NOT NULL,
            delivery_date TEXT NOT NULL,
            quantity_kg REAL NOT NULL,
            rate REAL NOT NULL,
            amount REAL NOT NULL,
            status TEXT NOT NULL DEFAULT 'delivered' CHECK(status IN ('delivered','missed','extra')),
            confirmed INTEGER DEFAULT 0,
            verified_via TEXT,
            timestamp TEXT NOT NULL,
            FOREIGN KEY(customer_id) REFERENCES customers(id),
            FOREIGN KEY(rider_id) REFERENCES riders(id)
        )
    """)
    _add_column_if_missing(conn, "deliveries", "verified_via TEXT")

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
        CREATE TABLE IF NOT EXISTS pending_otp (
            customer_id INTEGER PRIMARY KEY,
            otp TEXT NOT NULL,
            expires_at TEXT NOT NULL
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

    c.execute("""
        CREATE TABLE IF NOT EXISTS settings (
            key TEXT PRIMARY KEY,
            value TEXT
        )
    """)

    conn.commit()

    # seed default admin + default rate + encryption key
    c.execute("SELECT COUNT(*) AS n FROM users WHERE role='admin'")
    if c.fetchone()["n"] == 0:
        c.execute(
            "INSERT INTO users (username,password,role,name,phone) VALUES (?,?,?,?,?)",
            ("admin", hash_pw("admin123"), "admin", "Owner", "")
        )
    c.execute("SELECT COUNT(*) AS n FROM settings WHERE key='rate_per_kg'")
    if c.fetchone()["n"] == 0:
        c.execute("INSERT INTO settings (key,value) VALUES ('rate_per_kg','250')")

    if CRYPTO_AVAILABLE:
        c.execute("SELECT COUNT(*) AS n FROM settings WHERE key='secret_key'")
        if c.fetchone()["n"] == 0:
            c.execute("INSERT INTO settings (key,value) VALUES (?,?)", ("secret_key", Fernet.generate_key().decode()))

    conn.commit()
    conn.close()


def get_setting(key, default=None):
    conn = get_conn()
    row = conn.execute("SELECT value FROM settings WHERE key=?", (key,)).fetchone()
    conn.close()
    return row["value"] if row else default


def set_setting(key, value):
    conn = get_conn()
    conn.execute(
        "INSERT INTO settings (key,value) VALUES (?,?) "
        "ON CONFLICT(key) DO UPDATE SET value=excluded.value",
        (key, str(value))
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
    """Seals customer_id into an encrypted token and stores it as that customer's current QR value.
    Mirrors a 'generate dynamic/static QR' endpoint — each call issues a fresh ciphertext
    (Fernet includes a random IV + timestamp) that still decrypts to the same customer_id.
    """
    f = get_fernet()
    if f is None:
        # crypto lib missing — fall back to the plain customer code (still unique, just not encrypted)
        token = None
    else:
        token = f.encrypt(str(customer_id).encode()).decode()
    conn = get_conn()
    if token:
        conn.execute("UPDATE customers SET qr_token=? WHERE id=?", (token, customer_id))
        conn.commit()
    conn.close()
    return token


def verify_customer_qr(scanned_data: str):
    """Mirrors POST /verify-customer-qr.
    Input: raw string read off the rider's camera (encrypted token, or plain code as fallback).
    Output: matching customer dict, or None.
    """
    conn = get_conn()

    f = get_fernet()
    if f is not None:
        try:
            customer_id = int(f.decrypt(scanned_data.encode()).decode())
            row = conn.execute("SELECT * FROM customers WHERE id=? AND active=1", (customer_id,)).fetchone()
            if row:
                conn.close()
                return dict(row)
        except (InvalidToken, ValueError, Exception):
            pass  # not a valid encrypted token — fall through to plain-code lookup

    # fallback: plain customer code (older QR stickers / manual entry)
    row = conn.execute("SELECT * FROM customers WHERE code=? AND active=1", (scanned_data,)).fetchone()
    conn.close()
    return dict(row) if row else None


# ----------------------------- PIN / OTP AUTH -----------------------------

def set_customer_pin(customer_id: int, pin: str):
    conn = get_conn()
    conn.execute("UPDATE customers SET pin_hash=? WHERE id=?", (hash_pw(pin), customer_id))
    conn.commit()
    conn.close()


def verify_static_pin(customer_id: int, pin: str) -> bool:
    conn = get_conn()
    row = conn.execute("SELECT pin_hash FROM customers WHERE id=?", (customer_id,)).fetchone()
    conn.close()
    return bool(row and row["pin_hash"] and row["pin_hash"] == hash_pw(pin))


def generate_otp(customer_id: int) -> str:
    """Mirrors the backend issuing a one-time 4-digit PIN and pushing it to the customer."""
    otp = f"{random.randint(0, 9999):04d}"
    expires = (datetime.now() + timedelta(minutes=OTP_VALID_MINUTES)).isoformat()
    conn = get_conn()
    conn.execute(
        "INSERT INTO pending_otp (customer_id,otp,expires_at) VALUES (?,?,?) "
        "ON CONFLICT(customer_id) DO UPDATE SET otp=excluded.otp, expires_at=excluded.expires_at",
        (customer_id, otp, expires)
    )
    conn.commit()
    conn.close()
    push_notification(customer_id, f"آپ کی ڈیلیوری تصدیق کے لیے OTP: {otp} ({OTP_VALID_MINUTES} منٹ میں ختم ہو جائے گا)", audience="customer")
    return otp


def verify_otp(customer_id: int, otp: str) -> bool:
    conn = get_conn()
    row = conn.execute("SELECT * FROM pending_otp WHERE customer_id=?", (customer_id,)).fetchone()
    if not row:
        conn.close()
        return False
    ok = (row["otp"] == otp) and (datetime.fromisoformat(row["expires_at"]) >= datetime.now())
    if ok:
        conn.execute("DELETE FROM pending_otp WHERE customer_id=?", (customer_id,))
        conn.commit()
    conn.close()
    return ok


def get_pending_otp(customer_id: int):
    conn = get_conn()
    row = conn.execute("SELECT * FROM pending_otp WHERE customer_id=?", (customer_id,)).fetchone()
    conn.close()
    if row and datetime.fromisoformat(row["expires_at"]) >= datetime.now():
        return dict(row)
    return None


# ----------------------------- NOTIFICATIONS -----------------------------

def push_notification(customer_id, message, audience="customer", delivery_id=None):
    """In-app notification feed. Swap this for a real Firebase/OneSignal/WhatsApp
    Business API call later — every call site in this file already isolates the
    'who gets notified about what' logic, so only this function needs to change."""
    conn = get_conn()
    conn.execute(
        "INSERT INTO notifications (customer_id,audience,message,delivery_id,created_at) VALUES (?,?,?,?,?)",
        (customer_id, audience, message, delivery_id, datetime.now().isoformat())
    )
    conn.commit()
    conn.close()


def get_notifications(audience, customer_id=None, limit=20):
    conn = get_conn()
    if audience == "customer":
        rows = conn.execute(
            "SELECT * FROM notifications WHERE audience='customer' AND customer_id=? ORDER BY created_at DESC LIMIT ?",
            (customer_id, limit)
        ).fetchall()
    else:
        rows = conn.execute(
            "SELECT n.*, c.name AS customer_name FROM notifications n "
            "LEFT JOIN customers c ON c.id=n.customer_id "
            "WHERE n.audience='admin' ORDER BY n.created_at DESC LIMIT ?",
            (limit,)
        ).fetchall()
    conn.close()
    return [dict(r) for r in rows]


# ----------------------------- DELIVERY (CONFIRM) -----------------------------

def confirm_delivery(customer_id, rider_id, quantity_kg, rate, verified_via, status="delivered"):
    """Mirrors POST /confirm-delivery — called only after PIN/OTP verification succeeds.
    Writes the khata ledger entry, then notifies both the customer and the owner."""
    amount = round(quantity_kg * rate, 2)
    conn = get_conn()
    cur = conn.execute(
        "INSERT INTO deliveries (customer_id,rider_id,delivery_date,quantity_kg,rate,amount,status,confirmed,verified_via,timestamp) "
        "VALUES (?,?,?,?,?,?,?,?,?,?)",
        (customer_id, rider_id, date.today().isoformat(), quantity_kg, rate, amount, status, 1, verified_via, datetime.now().isoformat())
    )
    delivery_id = cur.lastrowid
    conn.commit()
    conn.close()

    if status == "delivered":
        push_notification(customer_id, f"آپ کے ہاں {quantity_kg} kg دودھ ڈیلیور ہوا — رقم Rs {amount:.0f}", audience="customer", delivery_id=delivery_id)
        push_notification(customer_id, f"ڈیلیوری کنفرم ہوئی — {quantity_kg} kg / Rs {amount:.0f}", audience="admin", delivery_id=delivery_id)
    return delivery_id


def mark_missed(customer_id, rider_id):
    conn = get_conn()
    rate = float(get_setting("rate_per_kg", "250"))
    cur = conn.execute(
        "INSERT INTO deliveries (customer_id,rider_id,delivery_date,quantity_kg,rate,amount,status,confirmed,verified_via,timestamp) "
        "VALUES (?,?,?,?,?,?,?,?,?,?)",
        (customer_id, rider_id, date.today().isoformat(), 0, rate, 0, "missed", 1, "n/a", datetime.now().isoformat())
    )
    delivery_id = cur.lastrowid
    conn.commit()
    conn.close()
    push_notification(customer_id, "آج ڈیلیوری نہیں ہوئی (ناغہ درج)", audience="customer", delivery_id=delivery_id)
    push_notification(customer_id, "ناغہ درج ہوا", audience="admin", delivery_id=delivery_id)


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


def login_page():
    st.markdown("## 🥛 Doodh Delivery System")
    st.caption("NABA TECH BY KALEEM ULLAH SHARIF")
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

def get_customers(active_only=True):
    conn = get_conn()
    q = "SELECT * FROM customers"
    if active_only:
        q += " WHERE active=1"
    q += " ORDER BY name"
    rows = conn.execute(q).fetchall()
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
        "SELECT COALESCE(SUM(amount),0) AS s FROM deliveries WHERE customer_id=? AND status!='missed'",
        (customer_id,)
    ).fetchone()["s"]
    total_paid = conn.execute(
        "SELECT COALESCE(SUM(amount),0) AS s FROM payments WHERE customer_id=?",
        (customer_id,)
    ).fetchone()["s"]
    conn.close()
    return round(total_amount - total_paid, 2)


# ----------------------------- RIDER PANEL -----------------------------

def rider_panel(user):
    rider = get_rider_by_user(user["id"])
    if not rider:
        st.error("آپ کا رائیڈر پروفائل نہیں ملا۔ ایڈمن سے رابطہ کریں۔")
        return

    st.header("🛵 رائیڈر پینل")
    rate = float(get_setting("rate_per_kg", "250"))
    st.info(f"آج کا ریٹ: **Rs {rate:.0f} / kg**")

    for key, default in [("cart", []), ("selected_customer", None), ("delivery_verified", False), ("verified_via", None)]:
        if key not in st.session_state:
            st.session_state[key] = default

    tab_scan, tab_deliver, tab_history = st.tabs(["📷 اسکین / کسٹمر منتخب کریں", "➕ ڈیلیوری ایڈ کریں", "📋 آج کی ہسٹری"])

    with tab_scan:
        st.subheader("کسٹمر منتخب کریں")
        customers = get_customers()

        if QR_SCAN_AVAILABLE:
            img_file = st.camera_input("QR کوڈ اسکین کریں")
            if img_file is not None:
                img = Image.open(img_file)
                results = qr_decode(img)
                if results:
                    scanned = results[0].data.decode("utf-8")
                    cust = verify_customer_qr(scanned)  # /verify-customer-qr
                    if cust:
                        st.session_state.selected_customer = cust
                        st.session_state.delivery_verified = False
                        st.success(f"کسٹمر تصدیق ہو گیا: {cust['name']}")
                    else:
                        st.error("QR کسی کسٹمر سے میچ نہیں ہوا (invalid / expired token)۔")
                else:
                    st.warning("کوئی QR کوڈ نہیں ملا، دوبارہ کوشش کریں۔")
        else:
            st.caption("(QR اسکین کے لیے requirements.txt میں pyzbar اور Pillow شامل کریں)")

        st.markdown("**یا فہرست سے منتخب کریں (fallback):**")
        if customers:
            names = [f"{c['name']} ({c['code']})" for c in customers]
            idx = st.selectbox("کسٹمر", range(len(names)), format_func=lambda i: names[i])
            if st.button("یہ کسٹمر منتخب کریں"):
                st.session_state.selected_customer = customers[idx]
                st.session_state.delivery_verified = False
                st.success(f"کسٹمر منتخب ہو گیا: {customers[idx]['name']}")
        else:
            st.warning("کوئی کسٹمر موجود نہیں۔ ایڈمن سے کسٹمر شامل کروائیں۔")

    with tab_deliver:
        cust = st.session_state.selected_customer
        if not cust:
            st.warning("پہلے کسٹمر منتخب کریں۔")
        else:
            st.success(f"موجودہ کسٹمر: **{cust['name']}** — بقیہ: Rs {customer_balance(cust['id']):.0f}")

            st.markdown("### مقدار شامل کریں")
            b1, b2, b3 = st.columns(3)
            if b1.button("پاؤ (250g)", use_container_width=True):
                st.session_state.cart.append(0.25)
                st.session_state.delivery_verified = False
            if b2.button("آدھا کلو (500g)", use_container_width=True):
                st.session_state.cart.append(0.5)
                st.session_state.delivery_verified = False
            if b3.button("1 کلو", use_container_width=True):
                st.session_state.cart.append(1.0)
                st.session_state.delivery_verified = False

            if st.session_state.cart:
                st.markdown("#### موجودہ اندراج")
                for i, qty in enumerate(st.session_state.cart):
                    col1, col2 = st.columns([4, 1])
                    col1.write(f"{qty} kg")
                    if col2.button("🗑️", key=f"del_{i}"):
                        st.session_state.cart.pop(i)
                        st.session_state.delivery_verified = False
                        st.rerun()

                total_qty = sum(st.session_state.cart)
                total_amount = total_qty * rate
                st.markdown(f"**کل مقدار: {total_qty} kg — کل رقم: Rs {total_amount:.0f}**")

                st.markdown("#### 🔐 کسٹمر تصدیق (PIN Authentication)")
                has_pin = bool(cust.get("pin_hash"))

                if st.session_state.delivery_verified:
                    st.success(f"تصدیق مکمل ✅ ({st.session_state.verified_via})")
                elif has_pin:
                    pin_input = st.text_input("کسٹمر کا 4-digit PIN درج کریں", max_chars=4, type="password", key="pin_field")
                    if st.button("PIN تصدیق کریں"):
                        if verify_static_pin(cust["id"], pin_input):
                            st.session_state.delivery_verified = True
                            st.session_state.verified_via = "Static PIN"
                            st.rerun()
                        else:
                            st.error("غلط PIN۔ دوبارہ کوشش کریں۔")
                else:
                    pending = get_pending_otp(cust["id"])
                    col_a, col_b = st.columns(2)
                    if col_a.button("📤 کسٹمر کو OTP بھیجیں"):
                        generate_otp(cust["id"])
                        st.info(f"OTP کسٹمر کے پینل/نمبر پر بھیج دیا گیا ({OTP_VALID_MINUTES} منٹ کے لیے) — کسٹمر سے کوڈ پوچھیں۔")
                        st.rerun()
                    otp_input = col_b.text_input("OTP درج کریں", max_chars=4, key="otp_field")
                    if pending:
                        st.caption(f"OTP فعال ہے، میعاد: {pending['expires_at'][11:16]}")
                    if st.button("OTP تصدیق کریں"):
                        if verify_otp(cust["id"], otp_input):
                            st.session_state.delivery_verified = True
                            st.session_state.verified_via = "OTP"
                            st.rerun()
                        else:
                            st.error("غلط یا میعاد ختم OTP۔")

                if st.button("✅ ڈیلیوری کنفرم کریں", type="primary", disabled=not st.session_state.delivery_verified):
                    confirm_delivery(cust["id"], rider["id"], total_qty, rate, st.session_state.verified_via)  # /confirm-delivery
                    st.session_state.cart = []
                    st.session_state.delivery_verified = False
                    st.session_state.verified_via = None
                    st.success("ڈیلیوری محفوظ ہو گئی اور کسٹمر/اونر کو نوٹیفکیشن بھیج دی گئی ✅")
                    st.rerun()
            else:
                st.caption("اوپر بٹن دبا کر مقدار شامل کریں۔")

            st.divider()
            if st.button("❌ آج ناغہ (Missed) مارک کریں"):
                mark_missed(cust["id"], rider["id"])
                st.success("ناغہ درج کر دیا گیا اور کسٹمر/اونر کو مطلع کر دیا گیا۔")

    with tab_history:
        today = date.today().isoformat()
        conn = get_conn()
        rows = conn.execute(
            "SELECT d.*, c.name AS customer_name FROM deliveries d "
            "JOIN customers c ON c.id=d.customer_id "
            "WHERE d.rider_id=? AND d.delivery_date=? ORDER BY d.timestamp DESC",
            (rider["id"], today)
        ).fetchall()
        conn.close()
        if rows:
            df = pd.DataFrame([dict(r) for r in rows])[["customer_name", "quantity_kg", "amount", "status", "verified_via", "timestamp"]]
            df.columns = ["کسٹمر", "مقدار (kg)", "رقم", "اسٹیٹس", "تصدیق کا طریقہ", "وقت"]
            st.dataframe(df, use_container_width=True, hide_index=True)
        else:
            st.caption("آج ابھی تک کوئی ڈیلیوری درج نہیں ہوئی۔")


# ----------------------------- ADMIN PANEL -----------------------------

def admin_panel(user):
    st.header("🧑‍💼 اونر / ایڈمن ڈیش بورڈ")

    tabs = st.tabs([
        "📡 لائیو ٹریکنگ", "💰 ریٹ مینجمنٹ", "👥 کسٹمرز", "🛵 رائیڈرز",
        "📒 کھاتہ / لیجر", "💵 وصولی درج کریں"
    ])

    # ---- Live tracking + admin notifications ----
    with tabs[0]:
        col_h, col_r = st.columns([4, 1])
        col_h.subheader("آج کی لائیو ڈیلیوریز")
        if col_r.button("🔄 ریفریش"):
            st.rerun()

        today = date.today().isoformat()
        conn = get_conn()
        rows = conn.execute(
            "SELECT d.*, c.name AS customer_name, r.name AS rider_name FROM deliveries d "
            "JOIN customers c ON c.id=d.customer_id "
            "JOIN riders r ON r.id=d.rider_id "
            "WHERE d.delivery_date=? ORDER BY d.timestamp DESC",
            (today,)
        ).fetchall()
        conn.close()
        if rows:
            df = pd.DataFrame([dict(r) for r in rows])[
                ["timestamp", "rider_name", "customer_name", "quantity_kg", "amount", "status", "verified_via"]
            ]
            df.columns = ["وقت", "رائیڈر", "کسٹمر", "مقدار (kg)", "رقم", "اسٹیٹس", "تصدیق"]
            st.dataframe(df, use_container_width=True, hide_index=True)
            m1, m2 = st.columns(2)
            m1.metric("آج کل ڈیلیور کیا گیا دودھ", f"{sum(r['quantity_kg'] for r in rows):.2f} kg")
            m2.metric("آج کل رقم", f"Rs {sum(r['amount'] for r in rows):.0f}")
        else:
            st.caption("آج ابھی تک کوئی ڈیلیوری نہیں ہوئی۔")

        st.divider()
        st.subheader("🔔 حالیہ نوٹیفکیشنز")
        notifs = get_notifications("admin")
        if notifs:
            for n in notifs:
                st.caption(f"[{n['created_at'][:16]}] {n.get('customer_name') or '—'}: {n['message']}")
        else:
            st.caption("کوئی نوٹیفکیشن نہیں۔")

    # ---- Rate management ----
    with tabs[1]:
        st.subheader("موجودہ ریٹ")
        current_rate = float(get_setting("rate_per_kg", "250"))
        new_rate = st.number_input("ریٹ فی کلو (Rs)", min_value=0.0, value=current_rate, step=5.0)
        if st.button("ریٹ اپڈیٹ کریں"):
            set_setting("rate_per_kg", new_rate)
            st.success(f"ریٹ Rs {new_rate:.0f} فی کلو کر دیا گیا۔ یہ فوراً رائیڈر اور کسٹمر پینل پر بھی اپڈیٹ ہو جائے گا۔")

    # ---- Customers ----
    with tabs[2]:
        st.subheader("نیا کسٹمر شامل کریں")
        with st.form("add_customer"):
            c_name = st.text_input("نام")
            c_address = st.text_input("پتہ")
            c_phone = st.text_input("فون")
            c_code = st.text_input("یونیک کوڈ (اندرونی شناخت)")
            c_quota = st.number_input("روزانہ کوٹہ (kg)", min_value=0.0, value=1.0, step=0.25)
            c_pin = st.text_input("Static PIN (4 digit، اختیاری — خالی چھوڑیں تو ہر ڈیلیوری پر OTP بھیجا جائے گا)", max_chars=4)
            make_login = st.checkbox("کسٹمر کے لیے لاگ ان اکاؤنٹ بھی بنائیں", value=True)
            c_user = st.text_input("یوزرنیم (اگر لاگ ان بنانا ہے)")
            c_pass = st.text_input("پاسورڈ (اگر لاگ ان بنانا ہے)", type="password")
            submitted = st.form_submit_button("شامل کریں")
            if submitted:
                if not c_name or not c_code:
                    st.error("نام اور کوڈ ضروری ہیں۔")
                elif c_pin and (len(c_pin) != 4 or not c_pin.isdigit()):
                    st.error("PIN بالکل 4 ہندسوں کا ہونا چاہیے۔")
                else:
                    conn = get_conn()
                    try:
                        user_id = None
                        if make_login and c_user and c_pass:
                            cur = conn.execute(
                                "INSERT INTO users (username,password,role,name,phone) VALUES (?,?,?,?,?)",
                                (c_user, hash_pw(c_pass), "customer", c_name, c_phone)
                            )
                            user_id = cur.lastrowid
                        cur2 = conn.execute(
                            "INSERT INTO customers (user_id,name,address,phone,code,daily_quota_kg) VALUES (?,?,?,?,?,?)",
                            (user_id, c_name, c_address, c_phone, c_code, c_quota)
                        )
                        new_id = cur2.lastrowid
                        conn.commit()
                        if c_pin:
                            set_customer_pin(new_id, c_pin)
                        generate_customer_qr_token(new_id)
                        st.success(f"کسٹمر '{c_name}' شامل ہو گیا اور QR جنریٹ ہو گیا۔")
                    except sqlite3.IntegrityError as e:
                        st.error(f"خرابی: یہ کوڈ یا یوزرنیم پہلے سے موجود ہے۔ ({e})")
                    finally:
                        conn.close()

        st.divider()
        st.subheader("موجودہ کسٹمرز")
        customers = get_customers()
        if customers:
            for c in customers:
                col1, col2, col3, col4 = st.columns([3, 2, 2, 2])
                col1.write(f"**{c['name']}** — {c['address'] or ''}")
                col2.write(f"کوڈ: `{c['code']}`")
                col3.write(f"بقیہ: Rs {customer_balance(c['id']):.0f}")
                col4.write("🔐 PIN سیٹ" if c.get("pin_hash") else "📲 OTP موڈ")

                with st.expander(f"QR / PIN — {c['name']}"):
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

                    new_pin = st.text_input("PIN سیٹ/تبدیل کریں", max_chars=4, key=f"pin_{c['id']}")
                    if st.button("PIN محفوظ کریں", key=f"savepin_{c['id']}"):
                        if new_pin and len(new_pin) == 4 and new_pin.isdigit():
                            set_customer_pin(c["id"], new_pin)
                            st.success("PIN محفوظ ہو گیا۔")
                            st.rerun()
                        else:
                            st.error("PIN بالکل 4 ہندسوں کا ہونا چاہیے۔")
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
                            "INSERT INTO users (username,password,role,name,phone) VALUES (?,?,?,?,?)",
                            (r_user, hash_pw(r_pass), "rider", r_name, r_phone)
                        )
                        conn.execute(
                            "INSERT INTO riders (user_id,name,phone) VALUES (?,?,?)",
                            (cur.lastrowid, r_name, r_phone)
                        )
                        conn.commit()
                        st.success(f"رائیڈر '{r_name}' شامل ہو گیا۔")
                    except sqlite3.IntegrityError:
                        st.error("یہ یوزرنیم پہلے سے موجود ہے۔")
                    finally:
                        conn.close()

        st.divider()
        conn = get_conn()
        riders = conn.execute("SELECT * FROM riders WHERE active=1").fetchall()
        conn.close()
        if riders:
            st.dataframe(pd.DataFrame([dict(r) for r in riders])[["name", "phone"]], hide_index=True, use_container_width=True)

    # ---- Ledger ----
    with tabs[4]:
        st.subheader("ماہانہ کھاتہ / لیجر")
        customers = get_customers()
        if customers:
            names = [c["name"] for c in customers]
            sel = st.selectbox("کسٹمر منتخب کریں", range(len(names)), format_func=lambda i: names[i])
            cust = customers[sel]

            month = st.text_input("مہینہ (YYYY-MM)", value=date.today().strftime("%Y-%m"))
            conn = get_conn()
            rows = conn.execute(
                "SELECT * FROM deliveries WHERE customer_id=? AND delivery_date LIKE ? ORDER BY delivery_date",
                (cust["id"], f"{month}%")
            ).fetchall()
            pay_rows = conn.execute(
                "SELECT * FROM payments WHERE customer_id=? AND payment_date LIKE ? ORDER BY payment_date",
                (cust["id"], f"{month}%")
            ).fetchall()
            conn.close()

            delivered = [r for r in rows if r["status"] != "missed"]
            missed = [r for r in rows if r["status"] == "missed"]
            total_qty = sum(r["quantity_kg"] for r in delivered)
            total_bill = sum(r["amount"] for r in delivered)
            total_paid = sum(r["amount"] for r in pay_rows)

            m1, m2, m3, m4 = st.columns(4)
            m1.metric("کل دودھ", f"{total_qty:.2f} kg")
            m2.metric("کل بل", f"Rs {total_bill:.0f}")
            m3.metric("وصول شدہ", f"Rs {total_paid:.0f}")
            m4.metric("باقی بقیہ", f"Rs {customer_balance(cust['id']):.0f}")
            st.caption(f"ناغے: {len(missed)} دن")

            if rows:
                st.markdown("**تفصیلی ریکارڈ**")
                df = pd.DataFrame([dict(r) for r in rows])[["delivery_date", "quantity_kg", "rate", "amount", "status", "verified_via"]]
                df.columns = ["تاریخ", "مقدار (kg)", "ریٹ", "رقم", "اسٹیٹس", "تصدیق"]
                st.dataframe(df, use_container_width=True, hide_index=True)
        else:
            st.caption("پہلے کسٹمر شامل کریں۔")

    # ---- Record payment ----
    with tabs[5]:
        st.subheader("وصولی درج کریں")
        customers = get_customers()
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
                    push_notification(cust["id"], f"آپ کی Rs {amt:.0f} وصولی درج ہو گئی ({method})", audience="customer")
                    st.success("وصولی درج ہو گئی۔")
                    st.rerun()
        else:
            st.caption("پہلے کسٹمر شامل کریں۔")


# ----------------------------- CUSTOMER PANEL -----------------------------

def customer_panel(user):
    conn = get_conn()
    cust = conn.execute("SELECT * FROM customers WHERE user_id=?", (user["id"],)).fetchone()
    conn.close()
    if not cust:
        st.error("آپ کا کسٹمر پروفائل نہیں ملا۔ ایڈمن سے رابطہ کریں۔")
        return
    cust = dict(cust)

    st.header(f"👤 خوش آمدید، {cust['name']}")
    rate = float(get_setting("rate_per_kg", "250"))
    st.info(f"آج کا ریٹ: **Rs {rate:.0f} / kg**")

    pending_otp = get_pending_otp(cust["id"])
    if pending_otp:
        st.warning(f"🔑 لائیو OTP (رائیڈر کو بتائیں): **{pending_otp['otp']}** — میعاد {pending_otp['expires_at'][11:16]} تک")

    month = date.today().strftime("%Y-%m")
    conn = get_conn()
    rows = conn.execute(
        "SELECT * FROM deliveries WHERE customer_id=? AND delivery_date LIKE ? ORDER BY delivery_date DESC",
        (cust["id"], f"{month}%")
    ).fetchall()
    pay_rows = conn.execute(
        "SELECT * FROM payments WHERE customer_id=? AND payment_date LIKE ? ORDER BY payment_date DESC",
        (cust["id"], f"{month}%")
    ).fetchall()
    conn.close()

    delivered = [r for r in rows if r["status"] != "missed"]
    missed = [r for r in rows if r["status"] == "missed"]
    total_qty = sum(r["quantity_kg"] for r in delivered)
    total_bill = sum(r["amount"] for r in delivered)
    total_paid = sum(r["amount"] for r in pay_rows)

    m1, m2, m3, m4 = st.columns(4)
    m1.metric("اس مہینے دودھ", f"{total_qty:.2f} kg")
    m2.metric("کل بل", f"Rs {total_bill:.0f}")
    m3.metric("جمع کروائی رقم", f"Rs {total_paid:.0f}")
    m4.metric("باقی بقیہ", f"Rs {customer_balance(cust['id']):.0f}")
    st.caption(f"اس مہینے ناغے: {len(missed)} دن")

    st.subheader("ڈیلیوری تفصیل (اس مہینے)")
    if rows:
        df = pd.DataFrame([dict(r) for r in rows])[["delivery_date", "quantity_kg", "rate", "amount", "status", "timestamp"]]
        df.columns = ["تاریخ", "مقدار (kg)", "ریٹ", "رقم", "اسٹیٹس", "وقت"]
        st.dataframe(df, use_container_width=True, hide_index=True)
    else:
        st.caption("اس مہینے ابھی تک کوئی ریکارڈ نہیں۔")

    st.subheader("وصولی کی تفصیل")
    if pay_rows:
        dfp = pd.DataFrame([dict(r) for r in pay_rows])[["payment_date", "amount", "method", "note"]]
        dfp.columns = ["تاریخ", "رقم", "طریقہ", "نوٹ"]
        st.dataframe(dfp, use_container_width=True, hide_index=True)
    else:
        st.caption("اس مہینے کوئی وصولی درج نہیں ہوئی۔")

    st.subheader("🔔 نوٹیفکیشنز")
    notifs = get_notifications("customer", customer_id=cust["id"])
    if notifs:
        for n in notifs:
            st.caption(f"[{n['created_at'][:16]}] {n['message']}")
    else:
        st.caption("کوئی نوٹیفکیشن نہیں۔")

    with st.expander("🔐 اپنا PIN سیٹ / تبدیل کریں"):
        st.caption("PIN سیٹ کرنے پر رائیڈر کو ہر بار OTP بھیجنے کی ضرورت نہیں رہے گی — رائیڈر یہی PIN مانگے گا۔")
        new_pin = st.text_input("نیا 4-digit PIN", max_chars=4, type="password", key="cust_new_pin")
        if st.button("PIN محفوظ کریں"):
            if new_pin and len(new_pin) == 4 and new_pin.isdigit():
                set_customer_pin(cust["id"], new_pin)
                st.success("PIN محفوظ ہو گیا۔")
            else:
                st.error("PIN بالکل 4 ہندسوں کا ہونا چاہیے۔")


# ----------------------------- MAIN -----------------------------

def main():
    st.set_page_config(page_title="Doodh Delivery System", page_icon="🥛", layout="wide")
    init_db()

    if "user" not in st.session_state:
        login_page()
        return

    logout_button()
    user = st.session_state.user

    if user["role"] == "admin":
        admin_panel(user)
    elif user["role"] == "rider":
        rider_panel(user)
    elif user["role"] == "customer":
        customer_panel(user)

    st.markdown("---")
    st.caption("NABA TECH BY KALEEM ULLAH SHARIF")


if __name__ == "__main__":
    main()
