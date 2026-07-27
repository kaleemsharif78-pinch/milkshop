"""
Doodh Delivery System — NABA TECH BY KALEEM ULLAH SHARIF
Roles: Rider, Owner/Admin, Customer
Single-file Streamlit app backed by SQLite.
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

DB_PATH = "milk_delivery.db"

# ----------------------------- DB LAYER -----------------------------

def get_conn():
    conn = sqlite3.connect(DB_PATH, check_same_thread=False)
    conn.row_factory = sqlite3.Row
    conn.execute("PRAGMA foreign_keys = ON")
    return conn


def hash_pw(pw: str) -> str:
    return hashlib.sha256(pw.encode()).hexdigest()


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
            timestamp TEXT NOT NULL,
            FOREIGN KEY(customer_id) REFERENCES customers(id),
            FOREIGN KEY(rider_id) REFERENCES riders(id)
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
        CREATE TABLE IF NOT EXISTS settings (
            key TEXT PRIMARY KEY,
            value TEXT
        )
    """)

    conn.commit()

    # seed default admin + default rate
    c.execute("SELECT COUNT(*) AS n FROM users WHERE role='admin'")
    if c.fetchone()["n"] == 0:
        c.execute(
            "INSERT INTO users (username,password,role,name,phone) VALUES (?,?,?,?,?)",
            ("admin", hash_pw("admin123"), "admin", "Owner", "")
        )
    c.execute("SELECT COUNT(*) AS n FROM settings WHERE key='rate_per_kg'")
    if c.fetchone()["n"] == 0:
        c.execute("INSERT INTO settings (key,value) VALUES ('rate_per_kg','250')")

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


def get_customer_by_code(code):
    conn = get_conn()
    row = conn.execute("SELECT * FROM customers WHERE code=?", (code,)).fetchone()
    conn.close()
    return dict(row) if row else None


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
    today = date.today().isoformat()
    rate = float(get_setting("rate_per_kg", "250"))
    st.info(f"آج کا ریٹ: **Rs {rate:.0f} / kg**")

    if "cart" not in st.session_state:
        st.session_state.cart = []
    if "selected_customer" not in st.session_state:
        st.session_state.selected_customer = None

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
                    code = results[0].data.decode("utf-8")
                    cust = get_customer_by_code(code)
                    if cust:
                        st.session_state.selected_customer = cust
                        st.success(f"کسٹمر منتخب ہو گیا: {cust['name']}")
                    else:
                        st.error("یہ کوڈ کسی کسٹمر سے میچ نہیں ہوا۔")
                else:
                    st.warning("کوئی QR کوڈ نہیں ملا، دوبارہ کوشش کریں۔")
        else:
            st.caption("(QR اسکین کے لیے requirements.txt میں pyzbar اور Pillow شامل کریں)")

        st.markdown("**یا فہرست سے منتخب کریں:**")
        if customers:
            names = [f"{c['name']} ({c['code']})" for c in customers]
            idx = st.selectbox("کسٹمر", range(len(names)), format_func=lambda i: names[i])
            if st.button("یہ کسٹمر منتخب کریں"):
                st.session_state.selected_customer = customers[idx]
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
            if b2.button("آدھا کلو (500g)", use_container_width=True):
                st.session_state.cart.append(0.5)
            if b3.button("1 کلو", use_container_width=True):
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

                confirmed = st.checkbox("کسٹمر نے تصدیق کر دی (OTP / انگوٹھا / OK)")
                if st.button("✅ ڈیلیوری کنفرم کریں", type="primary", disabled=not confirmed):
                    conn = get_conn()
                    conn.execute(
                        "INSERT INTO deliveries (customer_id,rider_id,delivery_date,quantity_kg,rate,amount,status,confirmed,timestamp) "
                        "VALUES (?,?,?,?,?,?,?,?,?)",
                        (cust["id"], rider["id"], today, total_qty, rate, total_amount, "delivered", 1, datetime.now().isoformat())
                    )
                    conn.commit()
                    conn.close()
                    st.session_state.cart = []
                    st.success("ڈیلیوری محفوظ ہو گئی ✅")
                    st.rerun()
            else:
                st.caption("اوپر بٹن دبا کر مقدار شامل کریں۔")

            st.divider()
            if st.button("❌ آج ناغہ (Missed) مارک کریں"):
                conn = get_conn()
                conn.execute(
                    "INSERT INTO deliveries (customer_id,rider_id,delivery_date,quantity_kg,rate,amount,status,confirmed,timestamp) "
                    "VALUES (?,?,?,?,?,?,?,?,?)",
                    (cust["id"], rider["id"], today, 0, rate, 0, "missed", 1, datetime.now().isoformat())
                )
                conn.commit()
                conn.close()
                st.success("ناغہ درج کر دیا گیا۔")

    with tab_history:
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
        "📒 کھاتہ / لیجر", "💵 وصولی درج کریں"
    ])

    # ---- Live tracking ----
    with tabs[0]:
        st.subheader("آج کی لائیو ڈیلیوریز")
        if st.button("🔄 ریفریش"):
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
                ["timestamp", "rider_name", "customer_name", "quantity_kg", "amount", "status"]
            ]
            df.columns = ["وقت", "رائیڈر", "کسٹمر", "مقدار (kg)", "رقم", "اسٹیٹس"]
            st.dataframe(df, use_container_width=True, hide_index=True)
            st.metric("آج کل ڈیلیور کیا گیا دودھ", f"{sum(r['quantity_kg'] for r in rows):.2f} kg")
            st.metric("آج کل رقم", f"Rs {sum(r['amount'] for r in rows):.0f}")
        else:
            st.caption("آج ابھی تک کوئی ڈیلیوری نہیں ہوئی۔")

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
            c_code = st.text_input("یونیک کوڈ (بارکوڈ/QR ویلیو)")
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
                        conn.execute(
                            "INSERT INTO customers (user_id,name,address,phone,code,daily_quota_kg) VALUES (?,?,?,?,?,?)",
                            (user_id, c_name, c_address, c_phone, c_code, c_quota)
                        )
                        conn.commit()
                        st.success(f"کسٹمر '{c_name}' شامل ہو گیا۔")
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
                if QR_AVAILABLE:
                    with st.expander(f"QR کوڈ — {c['name']}"):
                        qr_img = qrcode.make(c['code'])
                        buf = io.BytesIO()
                        qr_img.save(buf, format="PNG")
                        st.image(buf.getvalue(), width=150)
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
                df = pd.DataFrame([dict(r) for r in rows])[["delivery_date", "quantity_kg", "rate", "amount", "status"]]
                df.columns = ["تاریخ", "مقدار (kg)", "ریٹ", "رقم", "اسٹیٹس"]
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
