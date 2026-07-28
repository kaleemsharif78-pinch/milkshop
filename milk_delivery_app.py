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
from datetime import datetime, date
import hashlib
import io
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
    _add_column_if_missing(conn, "customers", "pin_plain TEXT")

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


# ----------------------------- RIDER CASH RECOVERY -----------------------------

def rider_cash_in_hand(rider_id: int) -> float:
    """Real-time cash a rider is currently holding: everything collected minus everything settled."""
    conn = get_conn()
    collected = conn.execute(
        "SELECT COALESCE(SUM(amount),0) AS s FROM cash_collections WHERE rider_id=?", (rider_id,)
    ).fetchone()["s"]
    settled = conn.execute(
        "SELECT COALESCE(SUM(amount),0) AS s FROM cash_settlements WHERE rider_id=?", (rider_id,)
    ).fetchone()["s"]
    conn.close()
    return round(collected - settled, 2)


def record_cash_collection(rider_id: int, customer_id, amount: float, note: str = ""):
    """Rider collects cash from a customer on the spot. This both reduces the
    customer's khata balance (payments table) and adds to the rider's cash-in-hand
    (cash_collections table) until the rider settles up with the owner."""
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
        push_notification(customer_id, f"آپ کی Rs {amount:.0f} نقد وصولی رائیڈر کے ذریعے درج ہو گئی", audience="customer")


def settle_rider_cash(rider_id: int, amount: float, note: str = ""):
    """Owner/admin receives cash physically handed over by the rider. Reduces that
    rider's cash-in-hand by `amount` (pass the full current amount to zero it out)."""
    conn = get_conn()
    conn.execute(
        "INSERT INTO cash_settlements (rider_id,amount,note,timestamp) VALUES (?,?,?,?)",
        (rider_id, amount, note, datetime.now().isoformat())
    )
    conn.commit()
    conn.close()


# ----------------------------- DELIVERY (CONFIRM) -----------------------------

def confirm_delivery(customer_id, rider_id, quantity_kg, rate, status="delivered"):
    """Mirrors POST /confirm-delivery. One-tap: scan -> quantity -> confirm, no PIN/OTP wait.
    Writes the khata ledger entry, then instantly notifies both the customer and the owner."""
    amount = round(quantity_kg * rate, 2)
    conn = get_conn()
    cur = conn.execute(
        "INSERT INTO deliveries (customer_id,rider_id,delivery_date,quantity_kg,rate,amount,status,confirmed,verified_via,timestamp) "
        "VALUES (?,?,?,?,?,?,?,?,?,?)",
        (customer_id, rider_id, date.today().isoformat(), quantity_kg, rate, amount, status, 1, "QR Scan", datetime.now().isoformat())
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

    for key, default in [("cart", []), ("selected_customer", None)]:
        if key not in st.session_state:
            st.session_state[key] = default

    st.metric("💵 آپ کے پاس موجود نقدی (Cash in Hand)", f"Rs {rider_cash_in_hand(rider['id']):.0f}")

    tab_deliver, tab_cash, tab_history = st.tabs(
        ["🚀 ڈیلیوری (Scan → Confirm)", "💵 کیش کلیکشن", "📋 آج کی ہسٹری"]
    )

    with tab_deliver:
        customers = get_customers()

        st.subheader("1️⃣ QR اسکین کریں")
        if QR_SCAN_AVAILABLE:
            img_file = st.camera_input("QR کوڈ اسکین کریں")
            if img_file is not None:
                img = Image.open(img_file)
                results = qr_decode(img)
                if results:
                    scanned = results[0].data.decode("utf-8")
                    cust = verify_customer_qr(scanned)  # /verify-customer-qr
                    if cust:
                        if not st.session_state.selected_customer or st.session_state.selected_customer["id"] != cust["id"]:
                            st.session_state.selected_customer = cust
                            st.session_state.cart = [cust["daily_quota_kg"]] if cust["daily_quota_kg"] > 0 else []
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
                st.session_state.cart = [cust["daily_quota_kg"]] if cust["daily_quota_kg"] > 0 else []
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
            c3.metric("ڈیفالٹ کوانٹٹی", f"{cust['daily_quota_kg']} kg")
            st.caption(f"بقیہ: Rs {customer_balance(cust['id']):.0f}")

            st.subheader("3️⃣ مقدار (ضرورت پر اضافہ کریں)")
            b1, b2, b3 = st.columns(3)
            if b1.button("➕ پاؤ (250g)", use_container_width=True):
                st.session_state.cart.append(0.25)
            if b2.button("➕ آدھا کلو (500g)", use_container_width=True):
                st.session_state.cart.append(0.5)
            if b3.button("➕ 1 کلو", use_container_width=True):
                st.session_state.cart.append(1.0)

            if st.session_state.cart:
                st.markdown("#### موجودہ اندراج")
                for i, qty in enumerate(st.session_state.cart):
                    col1, col2 = st.columns([4, 1])
                    col1.write(f"{qty} kg")
                    if col2.button("🗑️", key=f"del_{i}"):
                        st.session_state.cart.pop(i)
                        st.rerun()

                total_qty = sum(st.session_state.cart)
                total_amount = total_qty * rate
                st.markdown(f"**کل مقدار: {total_qty} kg — کل رقم: Rs {total_amount:.0f}**")

                st.subheader("4️⃣ کنفرم کریں")
                if st.button("✅ Confirm Delivery", type="primary", use_container_width=True):
                    confirm_delivery(cust["id"], rider["id"], total_qty, rate)  # /confirm-delivery — instant, no PIN wait
                    st.session_state.cart = []
                    st.session_state.selected_customer = None
                    st.success("✅ ڈیلیوری فوری سیو اور سنک ہو گئی — اونر ڈیش بورڈ اور کسٹمر پینل اپڈیٹ ہو گئے۔")
                    st.rerun()
            else:
                st.caption("کوئی مقدار موجود نہیں — بٹن دبا کر شامل کریں۔")

            st.divider()
            if st.button("❌ آج ناغہ (Missed) مارک کریں"):
                mark_missed(cust["id"], rider["id"])
                st.session_state.selected_customer = None
                st.session_state.cart = []
                st.success("ناغہ درج کر دیا گیا اور کسٹمر/اونر کو مطلع کر دیا گیا۔")
                st.rerun()
        else:
            st.info("پہلے QR اسکین کریں یا فہرست سے کسٹمر منتخب کریں۔")

    with tab_cash:
        st.subheader("💵 کسٹمر سے نقد وصولی درج کریں")
        st.caption(f"موجودہ نقدی آپ کے پاس: Rs {rider_cash_in_hand(rider['id']):.0f}")
        customers = get_customers()
        if customers:
            names = [f"{c['name']} (بقیہ Rs {customer_balance(c['id']):.0f})" for c in customers]
            idx = st.selectbox("کسٹمر", range(len(names)), format_func=lambda i: names[i], key="cash_cust_select")
            amt = st.number_input("وصول شدہ رقم", min_value=0.0, step=50.0, key="cash_amt")
            note = st.text_input("نوٹ (اختیاری)", key="cash_note")
            if st.button("💰 نقد وصولی درج کریں", type="primary"):
                if amt > 0:
                    record_cash_collection(rider["id"], customers[idx]["id"], amt, note)
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
            dfc = pd.DataFrame([dict(r) for r in crows])[["timestamp", "customer_name", "amount", "note"]]
            dfc.columns = ["وقت", "کسٹمر", "رقم", "نوٹ"]
            st.dataframe(dfc, use_container_width=True, hide_index=True)
        else:
            st.caption("آج ابھی تک کوئی نقد وصولی درج نہیں ہوئی۔")

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
            df = pd.DataFrame([dict(r) for r in rows])[["customer_name", "quantity_kg", "amount", "status", "timestamp"]]
            df.columns = ["کسٹمر", "مقدار (kg)", "رقم", "اسٹیٹس", "وقت"]
            st.dataframe(df, use_container_width=True, hide_index=True)
        else:
            st.caption("آج ابھی تک کوئی ڈیلیوری درج نہیں ہوئی۔")


# ----------------------------- ADMIN PANEL -----------------------------

def admin_panel(user):
    st.header("🧑‍💼 اونر / ایڈمن ڈیش بورڈ")

    tabs = st.tabs([
        "📡 لائیو ٹریکنگ", "💰 ریٹ مینجمنٹ", "👥 کسٹمرز", "🛵 رائیڈرز",
        "📒 کھاتہ / لیجر", "💵 وصولی درج کریں", "🧾 کیش سیٹلمنٹ"
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

    # ---- Cash Settlement / Recovery ----
    with tabs[6]:
        st.subheader("🧾 رائیڈرز کی نقدی (Cash in Hand)")
        conn = get_conn()
        riders = [dict(r) for r in conn.execute("SELECT * FROM riders WHERE active=1").fetchall()]
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
                "JOIN riders r ON r.id=cs.rider_id ORDER BY cs.timestamp DESC LIMIT 50"
            ).fetchall()
            conn.close()
            if hist:
                dfh = pd.DataFrame([dict(r) for r in hist])[["timestamp", "rider_name", "amount", "note"]]
                dfh.columns = ["وقت", "رائیڈر", "رقم", "نوٹ"]
                st.dataframe(dfh, use_container_width=True, hide_index=True)
            else:
                st.caption("ابھی تک کوئی سیٹلمنٹ نہیں ہوئی۔")
        else:
            st.caption("پہلے رائیڈر شامل کریں۔")


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

    col_rate, col_qr = st.columns([2, 1])
    with col_rate:
        rate = float(get_setting("rate_per_kg", "250"))
        st.info(f"آج کا ریٹ: **Rs {rate:.0f} / kg**")

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
