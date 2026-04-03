import sqlite3
from fastapi import FastAPI, HTTPException
from fastapi.middleware.cors import CORSMiddleware
from pydantic import BaseModel
from typing import List, Optional
import datetime
import uvicorn

app = FastAPI()

# --- 1. SETTING CORS (Pintu Masuk dari Hosting Frontend) ---
app.add_middleware(
    CORSMiddleware, 
    allow_origins=["*"], 
    allow_methods=["*"], 
    allow_headers=["*"]
)

DB_FILE = "sweetescape.db"

# --- 2. DATABASE UTILITY ---
def dict_factory(cursor, row):
    d = {}
    for idx, col in enumerate(cursor.description): d[col[0]] = row[idx]
    return d

def get_db():
    conn = sqlite3.connect(DB_FILE)
    conn.row_factory = dict_factory
    return conn

# --- 3. INISIALISASI DATABASE ---
def init_db():
    conn = get_db(); c = conn.cursor()
    # Tabel User & Admin
    c.execute('''CREATE TABLE IF NOT EXISTS users (username TEXT PRIMARY KEY, password TEXT, status TEXT, role TEXT)''')
    # Tabel Produk
    c.execute('''CREATE TABLE IF NOT EXISTS products (id INTEGER PRIMARY KEY AUTOINCREMENT, name TEXT, price INTEGER, stock INTEGER, category TEXT, image TEXT)''')
    # Tabel Pesanan
    c.execute('''CREATE TABLE IF NOT EXISTS orders (id INTEGER PRIMARY KEY AUTOINCREMENT, customer_name TEXT, whatsapp TEXT, payment_method TEXT, proof_of_payment TEXT, details TEXT, total INTEGER, date TEXT, time TEXT, status TEXT DEFAULT 'Pending', shipping_type TEXT)''')
    # Tabel Pengaturan (QRIS, Bank, dll)
    c.execute('''CREATE TABLE IF NOT EXISTS settings (key TEXT PRIMARY KEY, value TEXT)''')
    # Tabel Log Stok
    c.execute('''CREATE TABLE IF NOT EXISTS stock_logs (id INTEGER PRIMARY KEY AUTOINCREMENT, product_name TEXT, type TEXT, qty INTEGER, date TEXT, time TEXT)''')
    
    # Cek Kolom Baru (Migrasi Ringan)
    try: c.execute("ALTER TABLE orders ADD COLUMN status TEXT DEFAULT 'Pending'")
    except: pass
    try: c.execute("ALTER TABLE orders ADD COLUMN shipping_type TEXT")
    except: pass

    # Buat User Admin Default jika belum ada
    c.execute("SELECT * FROM users WHERE username='admin'")
    if not c.fetchone(): 
        c.execute("INSERT INTO users (username, password, status, role) VALUES ('admin', '123', 'approved', 'owner')")
    
    # Inisialisasi Settings Default
    for key in ['qris_image', 'bank_info', 'webhook_url']:
        c.execute("SELECT * FROM settings WHERE key=?", (key,))
        if not c.fetchone(): c.execute("INSERT INTO settings (key, value) VALUES (?, '')", (key,))
        
    conn.commit(); conn.close()

init_db()

# --- 4. MODEL DATA (PYDANTIC) ---
class UserAuth(BaseModel): username: str; password: str
class ProductCreate(BaseModel): name: str; price: int; stock: int; category: str; image: str
class OrderItem(BaseModel): product_id: int; quantity: int
class OrderCreate(BaseModel): customer_name: str; whatsapp: str; payment_method: str; proof_of_payment: str = ""; details: str = ""; items: List[OrderItem]; total: int; shipping_type: str
class OrderStatusUpdate(BaseModel): status: str
class PaymentUpdate(BaseModel): qris: str; bank: str; webhook: str

# --- 5. ENDPOINT AUTH ---
@app.post("/register")
def register(user: UserAuth):
    try:
        conn = get_db(); c = conn.cursor()
        c.execute("INSERT INTO users (username, password, status, role) VALUES (?, ?, 'pending', 'admin')", (user.username, user.password))
        conn.commit(); conn.close(); return {"msg": "ok"}
    except:
        raise HTTPException(status_code=400, detail="Username sudah digunakan")

@app.post("/login")
def login(user: UserAuth):
    conn = get_db(); c = conn.cursor(); c.execute("SELECT * FROM users WHERE username=? AND password=?", (user.username, user.password))
    u = c.fetchone(); conn.close()
    if u:
        if u['status'] == 'pending': raise HTTPException(status_code=403, detail="Menunggu ACC Owner")
        return {"token": f"tk-{user.username}", "role": u['role']}
    raise HTTPException(status_code=401, detail="User tidak ditemukan")

@app.get("/users")
def get_users():
    conn = get_db(); c = conn.cursor(); c.execute("SELECT * FROM users"); u = c.fetchall(); conn.close(); return u

@app.put("/users/approve/{username}")
def approve_user(username: str):
    conn = get_db(); c = conn.cursor(); c.execute("UPDATE users SET status='approved' WHERE username=?", (username,)); conn.commit(); conn.close(); return {"msg": "ok"}

@app.delete("/users/{username}")
def delete_user(username: str):
    conn = get_db(); c = conn.cursor(); c.execute("DELETE FROM users WHERE username=?", (username,)); conn.commit(); conn.close(); return {"msg": "ok"}

# --- 6. ENDPOINT PRODUK ---
@app.get("/products")
def get_products():
    conn = get_db(); c = conn.cursor(); c.execute("SELECT * FROM products"); p = c.fetchall(); conn.close(); return p

@app.post("/products")
def add_product(p: ProductCreate):
    conn = get_db(); c = conn.cursor()
    c.execute("INSERT INTO products (name, price, stock, category, image) VALUES (?, ?, ?, ?, ?)", (p.name, p.price, p.stock, p.category, p.image))
    conn.commit(); conn.close(); return {"msg": "ok"}

@app.put("/products/{pid}")
def update_product(pid: int, p: ProductCreate):
    conn = get_db(); c = conn.cursor()
    c.execute("UPDATE products SET name=?, price=?, stock=?, category=?, image=? WHERE id=?", (p.name, p.price, p.stock, p.category, p.image, pid))
    conn.commit(); conn.close(); return {"msg": "ok"}

@app.delete("/products/{pid}")
def delete_product(pid: int):
    conn = get_db(); c = conn.cursor(); c.execute("DELETE FROM products WHERE id=?", (pid,)); conn.commit(); conn.close(); return {"msg": "ok"}

@app.post("/products/bulk")
def bulk_add_products(products: List[ProductCreate]):
    conn = get_db(); c = conn.cursor()
    for p in products:
        c.execute("INSERT INTO products (name, price, stock, category, image) VALUES (?, ?, ?, ?, ?)", (p.name, p.price, p.stock, p.category, p.image))
    conn.commit(); conn.close(); return {"msg": "ok"}

# --- 7. ENDPOINT PESANAN (LOGIKA PENGURANGAN STOK) ---
@app.post("/orders")
def create_order(o: OrderCreate):
    conn = get_db(); c = conn.cursor(); dt = datetime.datetime.now().strftime("%Y-%m-%d"); tm = datetime.datetime.now().strftime("%H:%M")
    try:
        items_detail = []
        for i in o.items:
            c.execute("SELECT name, stock FROM products WHERE id=?", (i.product_id,))
            prod = c.fetchone()
            if prod and prod['stock'] >= i.quantity:
                # Kurangi stok produk secara otomatis
                c.execute("UPDATE products SET stock=? WHERE id=?", (prod['stock'] - i.quantity, i.product_id))
                items_detail.append(f"{prod['name']} ({i.quantity}x)")
            else: raise Exception(f"Stok {prod['name'] if prod else 'Produk'} Habis!")
        
        full_details = f"{o.details} | " + ", ".join(items_detail)
        c.execute("INSERT INTO orders (customer_name, whatsapp, payment_method, proof_of_payment, details, total, date, time, status, shipping_type) VALUES (?, ?, ?, ?, ?, ?, ?, ?, 'Pending', ?)", 
                  (o.customer_name, o.whatsapp, o.payment_method, o.proof_of_payment, full_details, o.total, dt, tm, o.shipping_type))
        
        new_id = c.lastrowid; conn.commit(); return {"order_id": new_id}
    except Exception as e: 
        conn.rollback(); raise HTTPException(status_code=400, detail=str(e))
    finally: conn.close()

@app.get("/orders")
def get_orders():
    conn = get_db(); c = conn.cursor(); c.execute("SELECT * FROM orders ORDER BY id DESC"); o = c.fetchall(); conn.close(); return o

@app.put("/orders/{order_id}/status")
def update_order_status(order_id: int, data: OrderStatusUpdate):
    conn = get_db(); c = conn.cursor(); c.execute("UPDATE orders SET status=? WHERE id=?", (data.status, order_id)); conn.commit(); conn.close(); return {"msg": "ok"}

@app.get("/orders/{order_id}/status")
def get_order_status(order_id: int):
    conn = get_db(); c = conn.cursor(); c.execute("SELECT status, shipping_type FROM orders WHERE id=?", (order_id,))
    o = c.fetchone(); conn.close(); return o

# --- 8. SETTINGS & REPORT ---
@app.get("/settings/payment")
def get_pay_settings():
    conn = get_db(); c = conn.cursor(); c.execute("SELECT key, value FROM settings"); rows = c.fetchall(); conn.close()
    res = {r['key']: r['value'] for r in rows}
    return {"qris": res.get('qris_image'), "bank": res.get('bank_info'), "webhook": res.get('webhook_url')}

@app.post("/settings/payment")
def update_pay_settings(p: PaymentUpdate):
    conn = get_db(); c = conn.cursor()
    c.execute("UPDATE settings SET value=? WHERE key='qris_image'", (p.qris,))
    c.execute("UPDATE settings SET value=? WHERE key='bank_info'", (p.bank,))
    c.execute("UPDATE settings SET value=? WHERE key='webhook_url'", (p.webhook,))
    conn.commit(); conn.close(); return {"msg": "ok"}

@app.get("/reports/daily")
def get_report(date: str):
    conn = get_db(); c = conn.cursor()
    c.execute("SELECT SUM(total) as omzet, COUNT(*) as count FROM orders WHERE date=?", (date,))
    s = c.fetchone()
    c.execute("SELECT * FROM orders WHERE date=?", (date,)); ords = c.fetchall()
    conn.close(); return {"omzet": s['omzet'] or 0, "total_order": s['count'] or 0, "orders": ords}

if __name__ == "__main__":
    uvicorn.run(app, host="0.0.0.0", port=10000)