from fastapi import Request, FastAPI, Depends, HTTPException, status, Query, Form
from fastapi.security import HTTPBasic, HTTPBasicCredentials
from fastapi.responses import FileResponse, HTMLResponse, RedirectResponse
import os, base64, requests, tempfile, sqlite3, uuid
import traceback
import logging
from datetime import datetime, date, timedelta
from openpyxl import Workbook
from reportlab.lib.pagesizes import A4
from reportlab.pdfgen import canvas
from typing import Optional
from xml.etree.ElementTree import Element, SubElement, tostring

app = FastAPI(title="Trendyol Kar/Zarar + e-Arşiv Taslak (Sağlam)")

logger = logging.getLogger("app")
logging.basicConfig(level=logging.INFO)

LAST_ERROR = {"text": ""}

@app.middleware("http")
async def catch_exceptions(request: Request, call_next):
    try:
        return await call_next(request)
    except Exception:
        tb = traceback.format_exc()
        msg = f"ERROR on {request.method} {request.url}\n\n{tb}"
        LAST_ERROR["text"] = msg
        logger.error(msg)
        return PlainTextResponse("Internal Server Error\n\n" + tb, status_code=500)

@app.get("/debug/last-error", response_class=PlainTextResponse)
def debug_last_error():
    return LAST_ERROR["text"] or "No error captured yet. Open /app to reproduce."

security = HTTPBasic()

# =========================
# AYARLAR
# =========================
INVOICE_RATE = float(os.getenv("INVOICE_RATE", "0.10"))
PAGE_SIZE = int(os.getenv("TRENDYOL_PAGE_SIZE", "200"))

# Render güvenli yazma yolu: env yoksa otomatik /tmp kullan
DB_PATH = os.getenv("DB_PATH", "/tmp/data.db")

# Satıcı bilgileri (Portal için)
SELLER_TITLE = os.getenv("SELLER_TITLE", "UNVANINIZ")
SELLER_VKN = os.getenv("SELLER_VKN", "0000000000")
SELLER_TAX_OFFICE = os.getenv("SELLER_TAX_OFFICE", "VERGI_DAIRESI")
SELLER_ADDRESS = os.getenv("SELLER_ADDRESS", "ADRES")
SELLER_CITY = os.getenv("SELLER_CITY", "IL")
SELLER_DISTRICT = os.getenv("SELLER_DISTRICT", "ILCE")
SELLER_EMAIL = os.getenv("SELLER_EMAIL", "mail@ornek.com")

# =========================
# AUTH
# =========================
def panel_auth(credentials: HTTPBasicCredentials = Depends(security)):
    user = os.getenv("PANEL_USER")
    password = os.getenv("PANEL_PASS")
    if not user or not password:
        raise HTTPException(status_code=500, detail="PANEL_USER / PANEL_PASS env eksik")

    if credentials.username != user or credentials.password != password:
        raise HTTPException(
            status_code=status.HTTP_401_UNAUTHORIZED,
            detail="Yetkisiz",
            headers={"WWW-Authenticate": "Basic"},
        )

# =========================
# DB
# =========================
def db():
    conn = sqlite3.connect(DB_PATH)
    conn.row_factory = sqlite3.Row
    return conn

def init_db():
    conn = db()
    cur = conn.cursor()
    cur.execute("""
        CREATE TABLE IF NOT EXISTS invoices(
            id INTEGER PRIMARY KEY AUTOINCREMENT,
            order_number TEXT NOT NULL,
            invoice_uuid TEXT NOT NULL,
            issue_date TEXT NOT NULL,
            customer_name TEXT,
            customer_vkn_tckn TEXT,
            customer_address TEXT,
            customer_city TEXT,
            customer_district TEXT,
            currency TEXT,
            subtotal REAL,
            vat_rate REAL,
            vat_amount REAL,
            total REAL,
            status TEXT NOT NULL DEFAULT 'draft',
            created_at TEXT NOT NULL
        )
    """)
    cur.execute("""
        CREATE TABLE IF NOT EXISTS invoice_lines(
            id INTEGER PRIMARY KEY AUTOINCREMENT,
            invoice_id INTEGER NOT NULL,
            name TEXT,
            quantity REAL,
            unit_price REAL,
            line_total REAL,
            vat_rate REAL,
            FOREIGN KEY(invoice_id) REFERENCES invoices(id)
        )
    """)
    # aynı siparişe birden fazla taslak açılmasın diye index
    cur.execute("""
        CREATE INDEX IF NOT EXISTS idx_invoices_order_number ON invoices(order_number)
    """)

    cur.execute("""
        CREATE TABLE IF NOT EXISTS sku_costs (
            merchant_sku TEXT PRIMARY KEY,
            cost REAL NOT NULL,
            updated_at TEXT
        )
    """)

    conn.commit()


def get_cost_map() -> dict:
    conn = db()
    cur = conn.cursor()
    cur.execute("SELECT merchant_sku, cost FROM sku_costs")
    rows = cur.fetchall()
    return {r[0]: float(r[1]) for r in rows if r and r[0]}

def upsert_cost(merchant_sku: str, cost: float):
    merchant_sku = (merchant_sku or "").strip()
    if not merchant_sku:
        raise ValueError("merchant_sku boş olamaz")
    conn = db()
    cur = conn.cursor()
    cur.execute(
        "INSERT INTO sku_costs(merchant_sku, cost, updated_at) VALUES(?,?,?) "
        "ON CONFLICT(merchant_sku) DO UPDATE SET cost=excluded.cost, updated_at=excluded.updated_at",
        (merchant_sku, float(cost), datetime.now().isoformat(timespec="seconds")),
    )
    conn.commit()

def delete_cost(merchant_sku: str):
    merchant_sku = (merchant_sku or "").strip()
    if not merchant_sku:
        return
    conn = db()
    cur = conn.cursor()
    cur.execute("DELETE FROM sku_costs WHERE merchant_sku=?", (merchant_sku,))
    conn.commit()

    conn.close()

init_db()

# =========================
# HELPERS
# =========================
def _ms(dt: datetime) -> int:
    return int(dt.timestamp() * 1000)

def date_range_to_ms(start_str: str, end_str: str) -> tuple[int, int]:
    s = datetime.strptime(start_str, "%Y-%m-%d")
    e = datetime.strptime(end_str, "%Y-%m-%d")
    start_dt = datetime(s.year, s.month, s.day, 0, 0, 0)
    end_dt = datetime(e.year, e.month, e.day, 23, 59, 59, 999000)
    return _ms(start_dt), _ms(end_dt)

def _num(x, default=0.0) -> float:
    try:
        if x is None:
            return float(default)
        return float(x)
    except Exception:
        return float(default)

def pick(d: dict, keys: list[str], default=0.0) -> float:
    for k in keys:
        if isinstance(d, dict) and k in d and d.get(k) is not None:
            return _num(d.get(k), default)
    return _num(default)

def get_qty(line: dict) -> float:
    return pick(line, ["quantity", "qty", "amount", "count"], default=1.0) or 1.0

def get_sale_price(line: dict) -> float:
    price = pick(line, ["price", "amount", "lineGrossAmount", "totalPrice", "totalAmount"], default=0.0)
    if price and price > 0:
        return price
    unit = pick(line, ["lineUnitPrice", "unitPrice", "unitSalePrice", "sellingPrice"], default=0.0)
    return unit * get_qty(line)

def get_commission(line: dict) -> float:
    return pick(line, ["commission", "commissionAmount", "tyCommissionAmount", "commissionTotal"], default=0.0)

def parse_discounts(line: dict) -> tuple[float, float]:
    seller = pick(line, ["lineSellerDiscount", "sellerDiscountAmount", "sellerDiscount"], default=0.0)
    ty = pick(line, ["lineTyDiscount", "tyDiscount", "tyDiscountAmount"], default=0.0)
    details = line.get("discountDetails")
    if isinstance(details, list):
        for obj in details:
            if not isinstance(obj, dict):
                continue
            seller += pick(obj, ["lineItemSellerDiscount"], default=0.0)
            ty += pick(obj, ["lineItemTyDiscount"], default=0.0)
    return float(seller), float(ty)

def get_campaign_label(line: dict) -> str:
    scid = line.get("salesCampaignId")
    if scid is not None and str(scid).strip():
        return f"salesCampaignId:{scid}"
    return ""

# =========================
# TRENDYOL API
# =========================
def trendyol_headers() -> tuple[str, dict]:
    api_key = os.getenv("TRENDYOL_API_KEY")
    api_secret = os.getenv("TRENDYOL_API_SECRET")
    seller_id = os.getenv("TRENDYOL_SELLER_ID")

    if not api_key or not api_secret or not seller_id:
        raise HTTPException(status_code=500, detail="TRENDYOL_API_KEY/SECRET/SELLER_ID env eksik")

    auth = base64.b64encode(f"{api_key}:{api_secret}".encode()).decode()
    url = f"https://api.trendyol.com/sapigw/suppliers/{seller_id}/orders"
    headers = {
        "Authorization": f"Basic {auth}",
        "User-Agent": f"{seller_id} - Trendyol API",
    }
    return url, headers

def fetch_orders(
    start_ms: int | None = None,
    end_ms: int | None = None,
    order_number: str | None = None,
    max_pages: int = 300
) -> list[dict]:
    """
    Sağlam sayfalama + opsiyonel orderNumber filtresi.
    """
    url, headers = trendyol_headers()
    orders: list[dict] = []

    page = 0
    while True:
        params = {"page": page, "size": PAGE_SIZE}
        if start_ms is not None:
            params["startDate"] = start_ms
        if end_ms is not None:
            params["endDate"] = end_ms
        if order_number:
            params["orderNumber"] = str(order_number).strip()

        r = requests.get(url, headers=headers, params=params, timeout=60)
        if r.status_code >= 400:
            raise HTTPException(status_code=502, detail=f"Trendyol API hata: {r.status_code} - {r.text}")

        data = r.json() or {}
        content = data.get("content") or []
        if not content:
            break

        orders.extend(content)

        total_pages = data.get("totalPages")
        if isinstance(total_pages, int) and page >= (total_pages - 1):
            break

        page += 1
        if page >= max_pages:
            break

    return orders

def find_order_by_number(order_number: str) -> Optional[dict]:
    """
    1) Önce orderNumber filtresiyle hızlı dene (180 gün)
    2) Olmazsa 365 gün geniş aralık brute-force (limitli sayfalama)
    """
    order_number = str(order_number).strip()
    if not order_number:
        return None

    now = datetime.now()

    # 1) hızlı deneme: orderNumber filtresi + 180 gün
    start = now - timedelta(days=180)
    orders = fetch_orders(start_ms=_ms(start), end_ms=_ms(now), order_number=order_number, max_pages=30)
    for o in orders:
        if str(o.get("orderNumber") or "").strip() == order_number:
            return o

    # 2) geniş aralık: 365 gün (filter yoksa yakalasın diye)
    start2 = now - timedelta(days=365)
    orders2 = fetch_orders(start_ms=_ms(start2), end_ms=_ms(now), order_number=None, max_pages=300)
    for o in orders2:
        if str(o.get("orderNumber") or "").strip() == order_number:
            return o

    return None

# =========================
# KAR/ZARAR
# =========================
def calc_profit_for_line(line: dict) -> dict:
    sale = get_sale_price(line)
    commission = get_commission(line)
    seller_disc, ty_disc = parse_discounts(line)

    invoice_base = max(sale - seller_disc, 0.0)
    invoice = invoice_base * INVOICE_RATE

    total_deductions = commission + seller_disc + invoice
    net_profit = sale - total_deductions

    return {
        "kampanya": get_campaign_label(line),
        "adet": get_qty(line),
        "satis": round(sale, 2),
        "komisyon": round(commission, 2),
        "kargo": 0.0,
        "satici_indirim": round(seller_disc, 2),
        "trendyol_indirim": round(ty_disc, 2),
        f"fatura_%{int(INVOICE_RATE*100)}": round(invoice, 2),
        "toplam_kesinti": round(total_deductions, 2),
        "net_kar": round(net_profit, 2),
    }

# =========================
# E-ARŞİV TASLAK
# =========================
def extract_customer_from_order(order: dict) -> dict:
    inv = order.get("invoiceAddress") or {}
    name = (inv.get("fullName") or f"{inv.get('name','')} {inv.get('surname','')}".strip()).strip()
    vkn = str(inv.get("taxNumber") or inv.get("identityNumber") or "").strip()
    addr = (inv.get("fullAddress") or inv.get("address") or "").strip()
    city = (inv.get("city") or "").strip()
    district = (inv.get("district") or inv.get("town") or "").strip()
    fallback_name = (str(order.get("customerFirstName") or "") + " " + str(order.get("customerLastName") or "")).strip()

    return {
        "name": name or fallback_name,
        "vkn_tckn": vkn,
        "address": addr,
        "city": city,
        "district": district,
    }

def get_existing_invoice_id_by_order(order_no: str) -> Optional[int]:
    conn = db()
    row = conn.execute("SELECT id FROM invoices WHERE order_number=? ORDER BY id DESC LIMIT 1", (order_no,)).fetchone()
    conn.close()
    if row:
        return int(row["id"])
    return None

def create_invoice_draft_from_order(order: dict) -> int:
    order_no = str(order.get("orderNumber") or "").strip()
    if not order_no:
        raise HTTPException(400, "Sipariş numarası bulunamadı.")

    # ✅ aynı siparişe tekrar taslak açmayı engelle
    existing = get_existing_invoice_id_by_order(order_no)
    if existing:
        return existing

    customer = extract_customer_from_order(order)
    lines = order.get("lines") or []
    if not lines:
        raise HTTPException(400, "Sipariş satırı yok.")

    subtotal = 0.0
    invoice_lines = []
    for l in lines:
        qty = _num(l.get("quantity"), 1.0) or 1.0
        line_total = get_sale_price(l)
        unit_price = (line_total / qty) if qty else line_total
        subtotal += line_total
        invoice_lines.append({
            "name": l.get("productName") or "Ürün",
            "quantity": qty,
            "unit_price": round(unit_price, 2),
            "line_total": round(line_total, 2),
            "vat_rate": 10.0,
        })

    vat_rate = 10.0
    vat_amount = round(subtotal * (vat_rate / 100.0), 2)
    total = round(subtotal + vat_amount, 2)

    inv_uuid = str(uuid.uuid4())
    issue_date = date.today().isoformat()

    conn = db()
    cur = conn.cursor()
    cur.execute("""
        INSERT INTO invoices(
            order_number, invoice_uuid, issue_date,
            customer_name, customer_vkn_tckn, customer_address, customer_city, customer_district,
            currency, subtotal, vat_rate, vat_amount, total, status, created_at
        ) VALUES (?,?,?,?,?,?,?,?,?,?,?,?,?,?,?)
    """, (
        order_no, inv_uuid, issue_date,
        customer["name"], customer["vkn_tckn"], customer["address"], customer["city"], customer["district"],
        "TRY", round(subtotal, 2), vat_rate, vat_amount, total, "draft", datetime.now().isoformat()
    ))
    invoice_id = cur.lastrowid

    for il in invoice_lines:
        cur.execute("""
            INSERT INTO invoice_lines(invoice_id, name, quantity, unit_price, line_total, vat_rate)
            VALUES (?,?,?,?,?,?)
        """, (
            invoice_id, il["name"], il["quantity"], il["unit_price"], il["line_total"], il["vat_rate"]
        ))

    conn.commit()
    conn.close()
    return int(invoice_id)

def get_invoice(invoice_id: int) -> dict:
    conn = db()
    cur = conn.cursor()
    inv = cur.execute("SELECT * FROM invoices WHERE id=?", (invoice_id,)).fetchone()
    if not inv:
        conn.close()
        raise HTTPException(404, "Fatura bulunamadı.")
    lines = cur.execute("SELECT * FROM invoice_lines WHERE invoice_id=? ORDER BY id", (invoice_id,)).fetchall()
    conn.close()
    return {"invoice": dict(inv), "lines": [dict(x) for x in lines]}

def build_basic_ubl_xml(inv: dict, lines: list[dict]) -> bytes:
    root = Element("Invoice")
    SubElement(root, "UUID").text = inv["invoice_uuid"]
    SubElement(root, "IssueDate").text = inv["issue_date"]
    SubElement(root, "DocumentCurrencyCode").text = inv["currency"] or "TRY"

    sup = SubElement(root, "AccountingSupplierParty")
    sup_p = SubElement(sup, "Party")
    SubElement(sup_p, "PartyName").text = SELLER_TITLE
    SubElement(sup_p, "CompanyID").text = SELLER_VKN
    SubElement(sup_p, "TaxOffice").text = SELLER_TAX_OFFICE
    addr = SubElement(sup_p, "PostalAddress")
    SubElement(addr, "StreetName").text = SELLER_ADDRESS
    SubElement(addr, "CityName").text = SELLER_CITY
    SubElement(addr, "CitySubdivisionName").text = SELLER_DISTRICT
    SubElement(sup_p, "ElectronicMail").text = SELLER_EMAIL

    cus = SubElement(root, "AccountingCustomerParty")
    cus_p = SubElement(cus, "Party")
    SubElement(cus_p, "PartyName").text = inv.get("customer_name") or ""
    SubElement(cus_p, "CompanyID").text = inv.get("customer_vkn_tckn") or ""
    caddr = SubElement(cus_p, "PostalAddress")
    SubElement(caddr, "StreetName").text = inv.get("customer_address") or ""
    SubElement(caddr, "CityName").text = inv.get("customer_city") or ""
    SubElement(caddr, "CitySubdivisionName").text = inv.get("customer_district") or ""

    for i, l in enumerate(lines, start=1):
        il = SubElement(root, "InvoiceLine")
        SubElement(il, "ID").text = str(i)
        SubElement(il, "ItemName").text = l.get("name") or "Ürün"
        SubElement(il, "InvoicedQuantity").text = str(l.get("quantity") or 1)
        SubElement(il, "PriceAmount").text = f"{_num(l.get('unit_price'),0):.2f}"
        SubElement(il, "LineExtensionAmount").text = f"{_num(l.get('line_total'),0):.2f}"
        SubElement(il, "VATRate").text = f"{_num(l.get('vat_rate'),10):.2f}"

    SubElement(root, "TaxExclusiveAmount").text = f"{_num(inv.get('subtotal'),0):.2f}"
    SubElement(root, "TaxAmount").text = f"{_num(inv.get('vat_amount'),0):.2f}"
    SubElement(root, "PayableAmount").text = f"{_num(inv.get('total'),0):.2f}"

    return tostring(root, encoding="utf-8", method="xml")

def build_pdf(inv: dict, lines: list[dict]) -> str:
    tmp = tempfile.NamedTemporaryFile(delete=False, suffix=".pdf")
    c = canvas.Canvas(tmp.name, pagesize=A4)
    w, h = A4

    y = h - 50
    c.setFont("Helvetica-Bold", 14)
    c.drawString(40, y, "e-Arşiv Fatura (TASLAK)")
    y -= 25

    c.setFont("Helvetica", 10)
    c.drawString(40, y, f"UUID: {inv['invoice_uuid']}")
    y -= 15
    c.drawString(40, y, f"Tarih: {inv['issue_date']}")
    y -= 15
    c.drawString(40, y, f"Sipariş No: {inv['order_number']}")
    y -= 25

    c.setFont("Helvetica-Bold", 11)
    c.drawString(40, y, "Satıcı:")
    c.setFont("Helvetica", 10)
    y -= 15
    c.drawString(60, y, f"{SELLER_TITLE} / VKN: {SELLER_VKN} / VD: {SELLER_TAX_OFFICE}")
    y -= 15
    c.drawString(60, y, f"{SELLER_ADDRESS} {SELLER_DISTRICT}/{SELLER_CITY}")
    y -= 20

    c.setFont("Helvetica-Bold", 11)
    c.drawString(40, y, "Alıcı:")
    c.setFont("Helvetica", 10)
    y -= 15
    c.drawString(60, y, f"{inv.get('customer_name','')} / VKN-TCKN: {inv.get('customer_vkn_tckn','')}")
    y -= 15
    c.drawString(60, y, f"{inv.get('customer_address','')} {inv.get('customer_district','')}/{inv.get('customer_city','')}")
    y -= 25

    c.setFont("Helvetica-Bold", 9)
    c.drawString(40, y, "Ürün")
    c.drawString(280, y, "Adet")
    c.drawString(330, y, "Birim")
    c.drawString(400, y, "Tutar")
    y -= 10
    c.line(40, y, 550, y)
    y -= 15

    c.setFont("Helvetica", 9)
    for l in lines:
        name = (l.get("name") or "")[:45]
        c.drawString(40, y, name)
        c.drawRightString(310, y, f"{_num(l.get('quantity'),1):.2f}")
        c.drawRightString(380, y, f"{_num(l.get('unit_price'),0):.2f}")
        c.drawRightString(520, y, f"{_num(l.get('line_total'),0):.2f}")
        y -= 14
        if y < 120:
            c.showPage()
            y = h - 50
            c.setFont("Helvetica", 9)

    y -= 10
    c.line(40, y, 550, y)
    y -= 20

    c.setFont("Helvetica-Bold", 10)
    c.drawRightString(520, y, f"Ara Toplam: {_num(inv.get('subtotal'),0):.2f} {inv.get('currency','TRY')}")
    y -= 15
    c.drawRightString(520, y, f"KDV (%10): {_num(inv.get('vat_amount'),0):.2f} {inv.get('currency','TRY')}")
    y -= 15
    c.drawRightString(520, y, f"Genel Toplam: {_num(inv.get('total'),0):.2f} {inv.get('currency','TRY')}")
    y -= 20
    c.setFont("Helvetica", 9)
    c.drawString(40, y, "Not: Bu belge taslaktır. GİB e-Arşiv Portal’da imzalanıp kesilecektir.")

    c.save()
    return tmp.name

# =========================
# UI
# =========================
def ui_shell(title: str, body: str, active: str = "dashboard") -> str:
    """Modern, sidebar'lı tek layout."""

    def nav_item(key: str, label: str, href: str, icon: str) -> str:
        cls = "bg-orange-50 text-orange-700 border-orange-200" if key == active else "hover:bg-slate-50 text-slate-700 border-transparent"
        return (
            f'<a href="{href}" class="flex items-center gap-3 px-3 py-2 rounded-xl border {cls}">'
            f'<span class="w-9 h-9 rounded-xl bg-white border flex items-center justify-center">{icon}</span>'
            f'<span class="font-semibold">{label}</span>'
            f'</a>'
        )

    sidebar = f"""
      <div class="hidden lg:block lg:w-72">
        <div class="sticky top-4 space-y-4">
          <div class="p-4 rounded-2xl bg-white border shadow-sm">
            <div class="flex items-center gap-3">
              <div class="w-10 h-10 rounded-xl bg-orange-500"></div>
              <div>
                <div class="font-extrabold text-lg leading-tight">Trendyol Panel</div>
                <div class="text-xs text-slate-500">Kâr/Zarar + e-Arşiv Taslak</div>
              </div>
            </div>
          </div>

          <div class="p-3 rounded-2xl bg-white border shadow-sm space-y-2">
            {nav_item("dashboard","Dashboard","/app","📊")}
            {nav_item("orders","Siparişler","/app/orders","🧾")}
            {nav_item("profit","Kârlılık","/app/profit","💰")}
            {nav_item("pricing","Fiyat / Hedef","/app/pricing","🏷️")}
            {nav_item("payouts","Hakediş","/app/payouts","🏦")}
            {nav_item("returns","İadeler","/app/returns","↩️")}
            {nav_item("campaigns","Kampanyalar","/app/campaigns","🎯")}
            {nav_item("invoices","Faturalar","/app/invoices","🧿")}
            {nav_item("settings","Ayarlar","/app/settings","⚙️")}
          </div>

          <div class="p-4 rounded-2xl bg-white border shadow-sm">
            <div class="text-xs text-slate-500">Hızlı Linkler</div>
            <div class="mt-2 flex flex-wrap gap-2">
              <a class="px-3 py-2 rounded-xl bg-slate-900 text-white text-sm font-bold" href="/env">Env Check</a>
              <a class="px-3 py-2 rounded-xl bg-orange-500 text-white text-sm font-bold" href="/app/costs">Maliyet</a>
              <a class="px-3 py-2 rounded-xl bg-white border text-sm font-bold hover:bg-slate-50" href="/health">Health</a>
            </div>
          </div>
        </div>
      </div>
    """

    topbar = f"""
      <div class="lg:hidden sticky top-0 z-50 bg-slate-50/90 backdrop-blur border-b">
        <div class="max-w-6xl mx-auto p-3 flex items-center justify-between">
          <div class="flex items-center gap-2">
            <div class="w-9 h-9 rounded-xl bg-orange-500"></div>
            <div class="font-extrabold">Trendyol Panel</div>
          </div>
          <details class="relative">
            <summary class="list-none px-3 py-2 rounded-xl bg-white border shadow-sm cursor-pointer">Menü</summary>
            <div class="absolute right-0 mt-2 w-64 p-3 rounded-2xl bg-white border shadow-sm space-y-2">
              {nav_item("dashboard","Dashboard","/app","📊")}
              {nav_item("orders","Siparişler","/app/orders","🧾")}
              {nav_item("invoices","Faturalar","/app/invoices","🧿")}
              {nav_item("settings","Ayarlar","/app/settings","⚙️")}
            </div>
          </details>
        </div>
      </div>
    """

    return f"""
<!doctype html>
<html lang="tr">
<head>
  <meta charset="utf-8" />
  <meta name="viewport" content="width=device-width, initial-scale=1" />
  <script src="https://cdn.tailwindcss.com"></script>
  <title>{title}</title>
</head>
<body class="bg-slate-50 text-slate-900">
  {topbar}
  <div class="max-w-6xl mx-auto p-4">
    <div class="lg:flex gap-4">
      {sidebar}

      <div class="flex-1">
        <div class="py-3">
          <div class="flex items-start justify-between gap-3">
            <div>
              <div class="text-2xl font-extrabold leading-tight">{title}</div>
              <div class="text-sm text-slate-500">Veriyi Trendyol API’den çekiyor, kâr/zarar hesaplıyor, e-Arşiv taslağı üretiyor.</div>
            </div>
            <div class="hidden md:flex items-center gap-2">
              <a class="px-3 py-2 rounded-xl bg-white border shadow-sm hover:bg-slate-50 font-bold" href="/app/orders">Sipariş Ara</a>
              <a class="px-3 py-2 rounded-xl bg-slate-900 text-white shadow-sm font-bold" href="/app">Raporla</a>
            </div>
          </div>
        </div>

        {body}

        <div class="text-xs text-slate-400 py-8">© {date.today().year} • build: ui-v2</div>
      </div>
    </div>
  </div>
</body>
</html>
"""

# =========================
# ENDPOINTS
# =========================
@app.get("/")
def root():
    return {"ok": True}

@app.get("/health")
def health():
    return {"status": "running"}

@app.get("/env")
def env_check():
    return {
        "TRENDYOL_API_KEY_SET": bool(os.getenv("TRENDYOL_API_KEY")),
        "TRENDYOL_API_SECRET_SET": bool(os.getenv("TRENDYOL_API_SECRET")),
        "TRENDYOL_SELLER_ID_SET": bool(os.getenv("TRENDYOL_SELLER_ID")),
        "PANEL_AUTH_SET": bool(os.getenv("PANEL_USER") and os.getenv("PANEL_PASS")),
        "INVOICE_RATE": INVOICE_RATE,
        "PAGE_SIZE": PAGE_SIZE,
        "DB_PATH": DB_PATH,
        "SELLER_TITLE_SET": SELLER_TITLE != "UNVANINIZ",
        "SELLER_VKN_SET": SELLER_VKN != "0000000000",
    }

@app.get("/debug/find-order")
def debug_find_order(orderNumber: str = Query(...), auth=Depends(panel_auth)):
    o = find_order_by_number(orderNumber)
    if not o:
        return {"found": False, "orderNumber": orderNumber}
    return {
        "found": True,
        "orderNumber": o.get("orderNumber"),
        "orderDate": o.get("orderDate"),
        "status": o.get("status"),
        "shipmentPackageId": o.get("shipmentPackageId"),
        "lines_count": len(o.get("lines") or []),
    }

@app.get("/report")
def report(start: str = Query(...), end: str = Query(...), auth=Depends(panel_auth)):
    start_ms, end_ms = date_range_to_ms(start, end)
    orders = fetch_orders(start_ms=start_ms, end_ms=end_ms)

    toplam_siparis = 0
    toplam_satis = toplam_komisyon = 0.0
    toplam_satici_indirim = toplam_trendyol_indirim = 0.0
    toplam_fatura = toplam_net = toplam_kesinti = 0.0

    for o in orders:
        od = o.get("orderDate")
        if isinstance(od, int) and not (start_ms <= od <= end_ms):
            continue

        toplam_siparis += 1
        for l in (o.get("lines") or []):
            calc = calc_profit_for_line(l)
            toplam_satis += calc["satis"]
            toplam_komisyon += calc["komisyon"]
            toplam_satici_indirim += calc["satici_indirim"]
            toplam_trendyol_indirim += calc["trendyol_indirim"]
            toplam_fatura += calc.get(f"fatura_%{int(INVOICE_RATE*100)}", 0.0)
            toplam_net += calc["net_kar"]
            toplam_kesinti += calc["toplam_kesinti"]

    return {
        "tarih": {"start": start, "end": end},
        "siparis": int(toplam_siparis),
        "satis_toplam": round(toplam_satis, 2),
        "komisyon_toplam": round(toplam_komisyon, 2),
        "kargo_toplam": 0.0,
        "satici_indirim_toplam": round(toplam_satici_indirim, 2),
        "trendyol_indirim_toplam": round(toplam_trendyol_indirim, 2),
        f"fatura_%{int(INVOICE_RATE*100)}_toplam": round(toplam_fatura, 2),
        "toplam_kesinti_toplam": round(toplam_kesinti, 2),
        "net_kar_toplam": round(toplam_net, 2),
    }

@app.get("/report/lines")
def report_lines(start: str = Query(...), end: str = Query(...), auth=Depends(panel_auth)):
    start_ms, end_ms = date_range_to_ms(start, end)
    orders = fetch_orders(start_ms=start_ms, end_ms=end_ms)

    rows = []
    for o in orders:
        od = o.get("orderDate")
        if isinstance(od, int) and not (start_ms <= od <= end_ms):
            continue

        order_no = o.get("orderNumber") or ""
        for l in (o.get("lines") or []):
            calc = calc_profit_for_line(l)
            rows.append({
                "Sipariş": order_no,
                "Ürün": l.get("productName") or "",
                "Kampanya": calc["kampanya"],
                "Satış": calc["satis"],
                "Komisyon": calc["komisyon"],
                "Kargo": 0.0,
                "Satıcı İndirim": calc["satici_indirim"],
                "Trendyol İndirim": calc["trendyol_indirim"],
                f"Fatura %10": calc.get("fatura_%10", 0.0),
                "Net Kâr": calc["net_kar"],
            })

    return {"tarih": {"start": start, "end": end}, "adet": len(rows), "rows": rows}

@app.get("/report/excel")
def report_excel(start: str = Query(...), end: str = Query(...), auth=Depends(panel_auth)):
    data = report_lines(start, end, auth=auth)
    rows = data["rows"]
    sumdata = report(start, end, auth=auth)

    wb = Workbook()
    ws1 = wb.active
    ws1.title = "Ozet"
    ws1.append(["Alan", "Tutar"])
    ws1.append(["Start", sumdata["tarih"]["start"]])
    ws1.append(["End", sumdata["tarih"]["end"]])
    for k, v in sumdata.items():
        if k == "tarih":
            continue
        ws1.append([k, v])

    ws2 = wb.create_sheet("Detay")
    if rows:
        headers = list(rows[0].keys())
        ws2.append(headers)
        for r in rows:
            ws2.append([r.get(h, "") for h in headers])
    else:
        ws2.append(["Bu tarih aralığında veri bulunamadı."])

    tmp = tempfile.NamedTemporaryFile(delete=False, suffix=".xlsx")
    wb.save(tmp.name)
    return FileResponse(tmp.name, filename=f"trendyol_kar_zarar_{start}_to_{end}.xlsx")

# =========================
# APP UI
# =========================
@app.get("/app", response_class=HTMLResponse)
def app_dashboard(auth=Depends(panel_auth)):
    today = date.today()
    week_ago = today - timedelta(days=6)

    body_template = """
    <div class="grid md:grid-cols-4 gap-3">
      <div class="p-4 rounded-2xl bg-white border shadow-sm">
        <div class="text-xs text-slate-500">Sipariş</div>
        <div class="text-3xl font-extrabold" id="k1">-</div>
      </div>
      <div class="p-4 rounded-2xl bg-white border shadow-sm">
        <div class="text-xs text-slate-500">Satış</div>
        <div class="text-3xl font-extrabold" id="k2">-</div>
      </div>
      <div class="p-4 rounded-2xl bg-white border shadow-sm">
        <div class="text-xs text-slate-500">Toplam Kesinti</div>
        <div class="text-3xl font-extrabold" id="k3">-</div>
      </div>
      <div class="p-4 rounded-2xl bg-white border shadow-sm">
        <div class="text-xs text-slate-500">Net Kâr</div>
        <div class="text-3xl font-extrabold" id="k4">-</div>
      </div>
    </div>

    <div class="mt-4 grid lg:grid-cols-3 gap-3">
      <div class="lg:col-span-2 p-4 rounded-2xl bg-white border shadow-sm">
        <div class="flex flex-wrap gap-3 items-end justify-between">
          <div class="flex flex-wrap gap-3 items-end">
            <div>
              <div class="text-xs text-slate-500 mb-1">Başlangıç</div>
              <input id="start" type="date" value="__START__" class="px-3 py-2 rounded-xl border bg-slate-50"/>
            </div>
            <div>
              <div class="text-xs text-slate-500 mb-1">Bitiş</div>
              <input id="end" type="date" value="__END__" class="px-3 py-2 rounded-xl border bg-slate-50"/>
            </div>
            <button onclick="loadAll()" class="px-4 py-2 rounded-xl bg-orange-500 text-white font-extrabold shadow-sm">Raporu Getir</button>
          </div>
          <div class="flex gap-2">
            <a id="excel" class="px-4 py-2 rounded-xl bg-slate-900 text-white font-extrabold shadow-sm" href="#">Excel İndir</a>
            <a class="px-4 py-2 rounded-xl bg-white border font-extrabold hover:bg-slate-50" href="/app/orders">Sipariş Ara</a>
          </div>
        </div>

        <div class="mt-4 overflow-auto rounded-xl border">
          <table class="min-w-full text-sm">
            <thead class="bg-slate-100 sticky top-0">
              <tr>
                <th class="text-left p-2">Sipariş</th>
                <th class="text-left p-2">Ürün</th>
                <th class="text-left p-2">Kampanya</th>
                <th class="text-right p-2">Satış</th>
                <th class="text-right p-2">Komisyon</th>
                <th class="text-right p-2">Satıcı İnd.</th>
                <th class="text-right p-2">Fatura %10</th>
                <th class="text-right p-2">Net</th>
                <th class="text-left p-2">e-Arşiv</th>
              </tr>
            </thead>
            <tbody id="tb" class="divide-y bg-white">
              <tr><td class="p-3 text-slate-500" colspan="9">Tarih seç → <b>Raporu Getir</b></td></tr>
            </tbody>
          </table>
        </div>
      </div>

      <div class="p-4 rounded-2xl bg-white border shadow-sm">
        <div class="font-extrabold">Hızlı Özet</div>
        <div class="text-xs text-slate-500">Net kâr / satış / komisyon / fatura toplamı.</div>
        <div class="mt-3">
          <canvas id="c1" height="160"></canvas>
        </div>
        <div class="mt-3 text-xs text-slate-500">
          İpucu: Fatura taslağı için tablodaki <b>Taslak</b> butonuna bas.
        </div>
      </div>
    </div>

<script src="https://cdn.jsdelivr.net/npm/chart.js@4.4.1/dist/chart.umd.min.js"></script>
<script>
function money(x){
  const n = Number(x||0);
  return n.toLocaleString('tr-TR',{minimumFractionDigits:2, maximumFractionDigits:2});
}

let chart1 = null;
function setChart(net, sales, comm, inv){
  const ctx = document.getElementById('c1');
  const data = {
    labels: ['Net Kâr','Satış','Komisyon','Fatura'],
    datasets: [{ label: 'Tutar (TRY)', data: [net, sales, comm, inv] }]
  };
  if(chart1){ chart1.destroy(); }
  chart1 = new Chart(ctx, { type: 'bar', data });
}

async function loadAll(){
  const s = document.getElementById('start').value;
  const e = document.getElementById('end').value;
  document.getElementById('excel').href = `/report/excel?start=${encodeURIComponent(s)}&end=${encodeURIComponent(e)}`;

  const r1 = await fetch(`/report?start=${encodeURIComponent(s)}&end=${encodeURIComponent(e)}`);
  const sum = await r1.json();
  document.getElementById('k1').innerText = sum.siparis ?? '-';
  document.getElementById('k2').innerText = money(sum.satis_toplam);
  document.getElementById('k3').innerText = money(sum.toplam_kesinti_toplam);
  document.getElementById('k4').innerText = money(sum.net_kar_toplam);

  setChart(sum.net_kar_toplam||0, sum.satis_toplam||0, sum.komisyon_toplam||0, sum['fatura_%10_toplam']||0);

  const r2 = await fetch(`/report/lines?start=${encodeURIComponent(s)}&end=${encodeURIComponent(e)}`);
  const det = await r2.json();
  const tb = document.getElementById('tb');
  tb.innerHTML = '';
  const rows = det.rows||[];
  if(!rows.length){
    tb.innerHTML = `<tr><td class="p-3 text-slate-500" colspan="9">Bu aralıkta satır yok.</td></tr>`;
    return;
  }
  rows.forEach(row=>{
    const tr = document.createElement('tr');
    const orderNo = row['Sipariş'] || '';
    tr.innerHTML = `
      <td class="p-2 whitespace-nowrap font-semibold">${orderNo}</td>
      <td class="p-2 min-w-[240px]">${row['Ürün']||''}</td>
      <td class="p-2 text-slate-500">${row['Kampanya']||''}</td>
      <td class="p-2 text-right">${money(row['Satış'])}</td>
      <td class="p-2 text-right">${money(row['Komisyon'])}</td>
      <td class="p-2 text-right">${money(row['Satıcı İndirim'])}</td>
      <td class="p-2 text-right">${money(row['Fatura %10'])}</td>
      <td class="p-2 text-right font-extrabold">${money(row['Net Kâr'])}</td>
      <td class="p-2">
        <form method="post" action="/invoice/draft">
          <input type="hidden" name="orderNumber" value="${orderNo}"/>
          <button class="px-3 py-1.5 rounded-xl bg-white border hover:bg-slate-50 font-bold" type="submit">Taslak</button>
        </form>
      </td>
    `;
    tb.appendChild(tr);
  });
}
</script>
"""
    body = body_template.replace("__START__", week_ago.isoformat()).replace("__END__", today.isoformat()).replace("__ROWS_LOSS__", rows_loss or "<tr><td class='p-3 text-slate-500' colspan='4'>Yok</td></tr>").replace("__ROWS_PROFIT__", rows_profit or "<tr><td class='p-3 text-slate-500' colspan='4'>Yok</td></tr>")
    return ui_shell("Dashboard", body, active="dashboard")

@app.get("/app/invoices", response_class=HTMLResponse)
def app_invoices(auth=Depends(panel_auth)):
    conn = db()
    rows = conn.execute("SELECT * FROM invoices ORDER BY id DESC LIMIT 200").fetchall()
    conn.close()

    trs = ""
    for r in rows:
        trs += f"""
        <tr class="border-b">
          <td class="p-2">{r['id']}</td>
          <td class="p-2">{r['order_number']}</td>
          <td class="p-2">{r['issue_date']}</td>
          <td class="p-2">{(r['customer_name'] or '')[:30]}</td>
          <td class="p-2 text-right">{r['total']:.2f}</td>
          <td class="p-2"><span class="px-2 py-1 rounded-lg bg-slate-100">{r['status']}</span></td>
          <td class="p-2 flex gap-2">
            <a class="px-3 py-1 rounded-lg bg-white border hover:bg-slate-50" href="/invoice/{r['id']}/pdf">PDF</a>
            <a class="px-3 py-1 rounded-lg bg-white border hover:bg-slate-50" href="/invoice/{r['id']}/xml">XML</a>
          </td>
        </tr>
        """

    body = f"""
    <div class="p-4 rounded-2xl bg-white border shadow-sm">
      <div class="flex items-center justify-between">
        <div>
          <div class="text-lg font-extrabold">Fatura Taslakları</div>
          <div class="text-xs text-slate-500">Portal için PDF + UBL XML indir.</div>
        </div>
      </div>
      <div class="mt-4 overflow-auto">
        <table class="min-w-full text-sm">
          <thead class="bg-slate-100">
            <tr>
              <th class="text-left p-2">ID</th>
              <th class="text-left p-2">Sipariş</th>
              <th class="text-left p-2">Tarih</th>
              <th class="text-left p-2">Müşteri</th>
              <th class="text-right p-2">Toplam</th>
              <th class="text-left p-2">Durum</th>
              <th class="text-left p-2">İndir</th>
            </tr>
          </thead>
          <tbody>
            {trs if trs else '<tr><td class="p-2 text-slate-500" colspan="7">Henüz taslak yok.</td></tr>'}
          </tbody>
        </table>
      </div>
    </div>
    """
    return ui_shell("Faturalar", body, active="invoices")


@app.get("/app/orders", response_class=HTMLResponse)
def app_orders(
    q: str = Query(default=""),
    days: int = Query(default=14, ge=1, le=365),
    auth=Depends(panel_auth)
):
    q = (q or "").strip()
    now = datetime.now()
    start = now - timedelta(days=int(days))

    orders = []
    err = ""
    try:
        if q:
            found = find_order_by_number(q)
            orders = [found] if found else []
        else:
            orders = fetch_orders(start_ms=_ms(start), end_ms=_ms(now), order_number=None, max_pages=30)
    except Exception as e:
        err = str(e)

    rows_html = ""
    for o in (orders or []):
        if not o:
            continue
        order_no = o.get("orderNumber") or ""
        status_ = o.get("status") or ""
        od = o.get("orderDate")
        dt = ""
        if isinstance(od, int):
            try:
                dt = datetime.fromtimestamp(int(od)/1000).strftime("%Y-%m-%d %H:%M")
            except Exception:
                dt = str(od)

        lines = o.get("lines") or []
        total_sale = 0.0
        total_net = 0.0
        for l in lines:
            c = calc_profit_for_line(l)
            total_sale += c["satis"]
            total_net += c["net_kar"]

        rows_html += f"""
        <tr class="border-b bg-white">
          <td class="p-2 font-semibold whitespace-nowrap">{order_no}</td>
          <td class="p-2 text-slate-500 whitespace-nowrap">{dt}</td>
          <td class="p-2"><span class="px-2 py-1 rounded-xl bg-slate-100 text-slate-700 text-xs font-bold">{status_}</span></td>
          <td class="p-2 text-right whitespace-nowrap">{total_sale:.2f}</td>
          <td class="p-2 text-right whitespace-nowrap font-extrabold">{total_net:.2f}</td>
          <td class="p-2">
            <form method="post" action="/invoice/draft">
              <input type="hidden" name="orderNumber" value="{order_no}"/>
              <button class="px-3 py-1.5 rounded-xl bg-orange-500 text-white font-extrabold shadow-sm" type="submit">Taslak Oluştur</button>
            </form>
          </td>
        </tr>
        """

    body = f"""
    <div class="p-4 rounded-2xl bg-white border shadow-sm">
      <div class="flex flex-wrap items-end justify-between gap-3">
        <div>
          <div class="font-extrabold text-lg">Siparişler</div>
          <div class="text-xs text-slate-500">Sipariş No yazarsan direkt bulur. Boş bırakırsan son {int(days)} gün listeler.</div>
        </div>
        <form class="flex flex-wrap gap-2 items-end" method="get" action="/app/orders">
          <div>
            <div class="text-xs text-slate-500 mb-1">Sipariş No</div>
            <input name="q" value="{q}" placeholder="örn: 10875234785" class="px-3 py-2 rounded-xl border bg-slate-50 w-64"/>
          </div>
          <div>
            <div class="text-xs text-slate-500 mb-1">Gün</div>
            <input name="days" value="{int(days)}" type="number" min="1" max="365" class="px-3 py-2 rounded-xl border bg-slate-50 w-24"/>
          </div>
          <button class="px-4 py-2 rounded-xl bg-slate-900 text-white font-extrabold shadow-sm" type="submit">Ara</button>
          <a class="px-4 py-2 rounded-xl bg-white border font-extrabold hover:bg-slate-50" href="/app/orders">Sıfırla</a>
        </form>
      </div>

      {("<div class='mt-3 p-3 rounded-xl bg-red-50 border border-red-200 text-red-700 text-sm'>Hata: "+err+"</div>") if err else ""}

      <div class="mt-4 overflow-auto rounded-xl border">
        <table class="min-w-full text-sm">
          <thead class="bg-slate-100">
            <tr>
              <th class="text-left p-2">Sipariş</th>
              <th class="text-left p-2">Tarih</th>
              <th class="text-left p-2">Durum</th>
              <th class="text-right p-2">Satış (Toplam)</th>
              <th class="text-right p-2">Net Kâr (Toplam)</th>
              <th class="text-left p-2">e-Arşiv</th>
            </tr>
          </thead>
          <tbody>
            {rows_html if rows_html else "<tr><td class='p-3 text-slate-500' colspan='6'>Kayıt yok.</td></tr>"}
          </tbody>
        </table>
      </div>
    </div>
    """
    return ui_shell("Siparişler", body, active="orders")


@app.get("/app/settings", response_class=HTMLResponse)
def app_settings(auth=Depends(panel_auth)):
    def mask(v: str) -> str:
        if not v:
            return "-"
        v = str(v)
        if len(v) <= 4:
            return "***"
        return v[:2] + "***" + v[-2:]

    body = f"""
    <div class="grid lg:grid-cols-2 gap-3">
      <div class="p-4 rounded-2xl bg-white border shadow-sm">
        <div class="font-extrabold text-lg">Bağlantı / Env</div>
        <div class="text-xs text-slate-500">Trendyol entegrasyonunun ayarlı olup olmadığını görürsün.</div>

        <div class="mt-4 grid grid-cols-2 gap-2 text-sm">
          <div class="p-3 rounded-xl bg-slate-50 border">
            <div class="text-xs text-slate-500">TRENDYOL_API_KEY</div>
            <div class="font-bold">{mask(os.getenv("TRENDYOL_API_KEY",""))}</div>
          </div>
          <div class="p-3 rounded-xl bg-slate-50 border">
            <div class="text-xs text-slate-500">TRENDYOL_API_SECRET</div>
            <div class="font-bold">{mask(os.getenv("TRENDYOL_API_SECRET",""))}</div>
          </div>
          <div class="p-3 rounded-xl bg-slate-50 border">
            <div class="text-xs text-slate-500">TRENDYOL_SELLER_ID</div>
            <div class="font-bold">{os.getenv("TRENDYOL_SELLER_ID","-") or "-"}</div>
          </div>
          <div class="p-3 rounded-xl bg-slate-50 border">
            <div class="text-xs text-slate-500">DB_PATH</div>
            <div class="font-bold">{DB_PATH}</div>
          </div>
        </div>

        <div class="mt-4">
          <a class="px-4 py-2 rounded-xl bg-white border font-extrabold hover:bg-slate-50" href="/env">Detaylı Env JSON</a>
        </div>
      </div>

      <div class="p-4 rounded-2xl bg-white border shadow-sm">
        <div class="font-extrabold text-lg">Satıcı Bilgileri (Portal)</div>
        <div class="text-xs text-slate-500">UBL XML + PDF üzerinde bu bilgiler basılır.</div>

        <div class="mt-4 grid grid-cols-2 gap-2 text-sm">
          <div class="p-3 rounded-xl bg-slate-50 border">
            <div class="text-xs text-slate-500">Ünvan</div>
            <div class="font-bold">{SELLER_TITLE}</div>
          </div>
          <div class="p-3 rounded-xl bg-slate-50 border">
            <div class="text-xs text-slate-500">VKN</div>
            <div class="font-bold">{SELLER_VKN}</div>
          </div>
          <div class="p-3 rounded-xl bg-slate-50 border">
            <div class="text-xs text-slate-500">Vergi Dairesi</div>
            <div class="font-bold">{SELLER_TAX_OFFICE}</div>
          </div>
          <div class="p-3 rounded-xl bg-slate-50 border">
            <div class="text-xs text-slate-500">E-posta</div>
            <div class="font-bold">{SELLER_EMAIL}</div>
          </div>
          <div class="p-3 rounded-xl bg-slate-50 border col-span-2">
            <div class="text-xs text-slate-500">Adres</div>
            <div class="font-bold">{SELLER_ADDRESS} {SELLER_DISTRICT}/{SELLER_CITY}</div>
          </div>
        </div>

        <div class="mt-4 text-xs text-slate-500">
          Bu alanları ENV ile değiştiriyorsun: SELLER_TITLE, SELLER_VKN, SELLER_TAX_OFFICE, SELLER_ADDRESS, SELLER_CITY, SELLER_DISTRICT, SELLER_EMAIL
        </div>
      </div>
    </div>
    """
    return ui_shell("Ayarlar", body, active="settings")


# =========================
# MELONTIK-LIKE PAGES (v3)
# =========================

def _try_fetch_lines(start_dt: datetime, end_dt: datetime, max_pages: int = 30):
    # Safely fetch orders and flatten lines with calculated profit.
    cost_map = get_cost_map()
    orders = fetch_orders(start_ms=_ms(start_dt), end_ms=_ms(end_dt), order_number=None, max_pages=max_pages)
    flat = []
    for o in orders or []:
        order_no = o.get("orderNumber") or ""
        od = o.get("orderDate")
        dt = None
        if isinstance(od, int):
            try:
                dt = datetime.fromtimestamp(int(od)/1000)
            except Exception:
                dt = None
        for l in (o.get("lines") or []):
            c = calc_profit_for_line(l)
            flat.append({
                "orderNumber": order_no,
                "orderDate": dt,
                "productName": l.get("productName") or "",
                "merchantSku": l.get("merchantSku") or l.get("merchantSkuId") or "",
                "sku": l.get("sku") or "",
                "campaign": l.get("salesCampaignId") or "",
                "qty": l.get("quantity") or 1,
                "unit_cost": float(cost_map.get((l.get("merchantSku") or l.get("merchantSkuId") or ""), 0.0)),
                **c
            })
    return flat

@app.get("/app/profit", response_class=HTMLResponse)
def app_profit(
    start: str = Query(default=""),
    end: str = Query(default=""),
    group: str = Query(default="sku"),
    q: str = Query(default=""),
    sort: str = Query(default="real_net"),
    auth=Depends(panel_auth)
):
    # Kârlılık ekranı: Ürün/SKU bazlı ve Sipariş bazlı özet.
    today = date.today()
    if not start:
        start_dt = datetime.combine(today - timedelta(days=30), datetime.min.time())
    else:
        start_dt = datetime.fromisoformat(start)
    if not end:
        end_dt = datetime.combine(today, datetime.max.time())
    else:
        end_dt = datetime.fromisoformat(end) + timedelta(days=1) - timedelta(milliseconds=1)

    group = group if group in ("sku", "order") else "sku"
    q = (q or "").strip().lower()
    sort = sort if sort in ("real_net","net","sales") else "real_net"

    err = ""
    rows = []
    summary = {"sales": 0.0, "net": 0.0, "comm": 0.0, "inv": 0.0, "disc": 0.0, "cost": 0.0, "real_net": 0.0, "count": 0}
    try:
        lines = _try_fetch_lines(start_dt, end_dt, max_pages=40)
        if q:
            filtered = []
            for x in lines:
                if (q in str(x.get('orderNumber','')).lower()) or (q in str(x.get('merchantSku','')).lower()) or (q in str(x.get('sku','')).lower()) or (q in str(x.get('productName','')).lower()):
                    filtered.append(x)
            lines = filtered
        summary["count"] = len(lines)
        for x in lines:
            summary["sales"] += x.get("satis", 0.0)
            summary["net"] += x.get("net_kar", 0.0)
            summary["comm"] += x.get("komisyon", 0.0)
            summary["inv"] += x.get("fatura", 0.0)
            summary["disc"] += x.get("satici_indirim", 0.0)
            summary["cost"] += float(x.get("unit_cost", 0.0)) * float(x.get("qty", 1) or 1)
            summary["real_net"] += x.get("net_kar", 0.0) - (float(x.get("unit_cost", 0.0)) * float(x.get("qty", 1) or 1))

        agg = {}
        if group == "sku":
            for x in lines:
                key = (x.get("merchantSku") or x.get("sku") or x.get("productName") or "Bilinmeyen")
                a = agg.setdefault(key, {"key": key, "qty": 0, "sales": 0.0, "net": 0.0, "comm": 0.0, "inv": 0.0, "disc": 0.0, "cost": 0.0, "real_net": 0.0})
                a["qty"] += int(x.get("qty", 1) or 1)
                a["sales"] += x.get("satis", 0.0)
                a["net"] += x.get("net_kar", 0.0)
                a["comm"] += x.get("komisyon", 0.0)
                a["inv"] += x.get("fatura", 0.0)
                a["disc"] += x.get("satici_indirim", 0.0)
                a["cost"] += float(x.get("unit_cost", 0.0)) * float(x.get("qty", 1) or 1)
                a["real_net"] += x.get("net_kar", 0.0) - (float(x.get("unit_cost", 0.0)) * float(x.get("qty", 1) or 1))
        else:
            for x in lines:
                key = x.get("orderNumber") or ""
                a = agg.setdefault(key, {"key": key, "qty": 0, "sales": 0.0, "net": 0.0, "comm": 0.0, "inv": 0.0, "disc": 0.0, "cost": 0.0, "real_net": 0.0})
                a["qty"] += int(x.get("qty", 1) or 1)
                a["sales"] += x.get("satis", 0.0)
                a["net"] += x.get("net_kar", 0.0)
                a["comm"] += x.get("komisyon", 0.0)
                a["inv"] += x.get("fatura", 0.0)
                a["disc"] += x.get("satici_indirim", 0.0)
                a["cost"] += float(x.get("unit_cost", 0.0)) * float(x.get("qty", 1) or 1)
                a["real_net"] += x.get("net_kar", 0.0) - (float(x.get("unit_cost", 0.0)) * float(x.get("qty", 1) or 1))

        rows = sorted(agg.values(), key=lambda r: r.get(sort, 0.0))
    except Exception as e:
        err = str(e)

    def tr_money(v: float) -> str:
        try:
            return f"{float(v):,.2f}".replace(",", "X").replace(".", ",").replace("X", ".")
        except Exception:
            return str(v)

    body_rows = ""
    for r in rows[-200:]:
        body_rows += f"""
        <tr class="border-b bg-white">
          <td class="p-2 font-semibold">{r['key']}</td>
          <td class="p-2 text-right">{r.get('qty', 0)}</td>
          <td class="p-2 text-right">{tr_money(r['sales'])}</td>
          <td class="p-2 text-right">{tr_money(r['comm'])}</td>
          <td class="p-2 text-right">{tr_money(r['disc'])}</td>
          <td class="p-2 text-right">{tr_money(r['inv'])}</td>
          <td class="p-2 text-right">{tr_money(r.get("cost",0.0))}</td>
          <td class="p-2 text-right font-extrabold">{tr_money(r.get("real_net",0.0))}</td>
          <td class="p-2 text-right">{tr_money(r['net'])}</td>
        </tr>
        """

    body = f"""
    <div class="grid md:grid-cols-5 gap-3">
      <div class="p-4 rounded-2xl bg-white border shadow-sm md:col-span-3">
        <div class="flex flex-wrap gap-2 items-end justify-between">
          <div>
            <div class="font-extrabold text-lg">Kârlılık</div>
            <div class="text-xs text-slate-500">Zararı yakala, kârı büyüt. (Seçili aralık)</div>
          </div>
          <form class="flex flex-wrap gap-2 items-end" method="get" action="/app/profit">
            <div>
              <div class="text-xs text-slate-500 mb-1">Başlangıç</div>
              <input name="start" type="date" value="{start_dt.date().isoformat()}" class="px-3 py-2 rounded-xl border bg-slate-50"/>
            </div>
            <div>
              <div class="text-xs text-slate-500 mb-1">Bitiş</div>
              <input name="end" type="date" value="{end_dt.date().isoformat()}" class="px-3 py-2 rounded-xl border bg-slate-50"/>
            </div>
            <div>
              <div class="text-xs text-slate-500 mb-1">Grupla</div>
              <select name="group" class="px-3 py-2 rounded-xl border bg-slate-50">
                <option value="sku" {"selected" if group=="sku" else ""}>Ürün/SKU</option>
                <option value="order" {"selected" if group=="order" else ""}>Sipariş</option>
              </select>
            </div>
            <div>
              <div class="text-xs text-slate-500 mb-1">Ara</div>
              <input name="q" value="{q}" placeholder="sku / ürün / sipariş" class="px-3 py-2 rounded-xl border bg-slate-50 w-56"/>
            </div>
            <div>
              <div class="text-xs text-slate-500 mb-1">Sırala</div>
              <select name="sort" class="px-3 py-2 rounded-xl border bg-slate-50">
                <option value="real_net" {"selected" if sort=="real_net" else ""}>Gerçek Net</option>
                <option value="net" {"selected" if sort=="net" else ""}>Net</option>
                <option value="sales" {"selected" if sort=="sales" else ""}>Satış</option>
              </select>
            </div>
            <button class="px-4 py-2 rounded-xl bg-slate-900 text-white font-extrabold shadow-sm" type="submit">Analiz</button>
          </form>
        </div>

        {("<div class='mt-3 p-3 rounded-xl bg-red-50 border border-red-200 text-red-700 text-sm'>Hata: "+err+"</div>") if err else ""}

        <div class="mt-4 overflow-auto rounded-xl border">
          <table class="min-w-full text-sm">
            <thead class="bg-slate-100 sticky top-0">
              <tr>
                <th class="text-left p-2">{'SKU / Ürün' if group=='sku' else 'Sipariş'}</th>
                <th class="text-right p-2">Adet</th>
                <th class="text-right p-2">Satış</th>
                <th class="text-right p-2">Komisyon</th>
                <th class="text-right p-2">İndirim</th>
                <th class="text-right p-2">Fatura</th>
                <th class="text-right p-2">Maliyet</th>
                <th class="text-right p-2">Gerçek Net</th>
                <th class="text-right p-2">Net</th>
              </tr>
            </thead>
            <tbody class="divide-y">
              {body_rows if body_rows else "<tr><td class='p-3 text-slate-500' colspan='9'>Kayıt yok.</td></tr>"}
            </tbody>
          </table>
        </div>

        <div class="mt-3 text-xs text-slate-500">
          Not: Bu net kâr hesabı mevcut formülünden geliyor (komisyon/indirim/fatura). Ürün maliyeti eklemek istersen bir sonraki adımda maliyet tablosu ekleriz.
        </div>
      </div>

      <div class="p-4 rounded-2xl bg-white border shadow-sm md:col-span-2">
        <div class="font-extrabold text-lg">Özet</div>
        <div class="mt-3 grid grid-cols-2 gap-2 text-sm">
          <div class="p-3 rounded-xl bg-slate-50 border">
            <div class="text-xs text-slate-500">Satır Sayısı</div>
            <div class="font-extrabold">{summary['count']}</div>
          </div>
          <div class="p-3 rounded-xl bg-slate-50 border">
            <div class="text-xs text-slate-500">Satış</div>
            <div class="font-extrabold">{tr_money(summary['sales'])}</div>
          </div>
          <div class="p-3 rounded-xl bg-slate-50 border">
            <div class="text-xs text-slate-500">Komisyon</div>
            <div class="font-extrabold">{tr_money(summary['comm'])}</div>
          </div>
          <div class="p-3 rounded-xl bg-slate-50 border">
            <div class="text-xs text-slate-500">İndirim</div>
            <div class="font-extrabold">{tr_money(summary['disc'])}</div>
          </div>
          <div class="p-3 rounded-xl bg-slate-50 border">
            <div class="text-xs text-slate-500">Fatura</div>
            <div class="font-extrabold">{tr_money(summary['inv'])}</div>
          </div>
          <div class="p-3 rounded-xl bg-slate-50 border">
            <div class="text-xs text-slate-500">Maliyet</div>
            <div class="font-extrabold">{tr_money(summary['cost'])}</div>
          </div>
          <div class="p-3 rounded-xl bg-orange-50 border border-orange-200">
            <div class="text-xs text-orange-700">Gerçek Net</div>
            <div class="font-extrabold text-orange-800">{tr_money(summary['real_net'])}</div>
          </div>
        </div>

        <div class="mt-4 p-3 rounded-xl bg-slate-900 text-white">
          <div class="font-extrabold">İpucu</div>
          <div class="text-xs opacity-80">Zarar eden SKU’ları yakala → fiyat/komisyon/indirim kaynaklı mı bak → hedef fiyat ekranından minimum kârlı fiyatı çıkar.</div>
        </div>
      </div>
    </div>
    """
    return ui_shell("Kârlılık", body, active="profit")


@app.get("/app/pricing", response_class=HTMLResponse)
def app_pricing(
    sku: str = Query(default=""),
    sale_price: float = Query(default=0.0, ge=0.0),
    cost: float = Query(default=0.0, ge=0.0),
    target_margin: float = Query(default=0.15, ge=0.0, le=5.0),
    commission_rate: float = Query(default=0.20, ge=0.0, le=1.0),
    auth=Depends(panel_auth)
):
    # Basit hedef fiyat hesabı (simülasyon)
    invoice_rate = float(INVOICE_RATE or 0.10)
    p = float(sale_price or 0.0)
    c = float(cost or 0.0)
    if (not c) and sku:
        c = float(get_cost_map().get(sku.strip(), 0.0))
    tr = float(target_margin or 0.0)
    cr = float(commission_rate or 0.0)

    denom = (1.0 - cr - invoice_rate - tr)
    min_price = None
    if denom > 0:
        min_price = c / denom

    def fm(x):
        try:
            return f"{float(x):,.2f}".replace(",", "X").replace(".", ",").replace("X", ".")
        except Exception:
            return str(x)

    body = f"""
    <div class="grid lg:grid-cols-3 gap-3">
      <div class="lg:col-span-2 p-4 rounded-2xl bg-white border shadow-sm">
        <div class="font-extrabold text-lg">Fiyat / Hedef (Simülasyon)</div>
        <div class="text-xs text-slate-500">Hızlı karar: bu ürünü kaça satmalıyım?</div>

        <form class="mt-4 grid md:grid-cols-5 gap-2 items-end" method="get" action="/app/pricing">
          <div>
            <div class="text-xs text-slate-500 mb-1">SKU (opsiyonel)</div>
            <input name="sku" value="{sku}" placeholder="örn: KPP12343" class="px-3 py-2 rounded-xl border bg-slate-50 w-full"/>
          </div>
          <div>
            <div class="text-xs text-slate-500 mb-1">Satış Fiyatı</div>
            <input name="sale_price" value="{p}" type="number" step="0.01" class="px-3 py-2 rounded-xl border bg-slate-50 w-full"/>
          </div>
          <div>
            <div class="text-xs text-slate-500 mb-1">Maliyet</div>
            <input name="cost" value="{c}" type="number" step="0.01" class="px-3 py-2 rounded-xl border bg-slate-50 w-full"/>
          </div>
          <div>
            <div class="text-xs text-slate-500 mb-1">Komisyon Oranı</div>
            <input name="commission_rate" value="{cr}" type="number" step="0.01" min="0" max="1" class="px-3 py-2 rounded-xl border bg-slate-50 w-full"/>
            <div class="text-[11px] text-slate-400 mt-1">0.20 = %20</div>
          </div>
          <div>
            <div class="text-xs text-slate-500 mb-1">Hedef Kâr Oranı</div>
            <input name="target_margin" value="{tr}" type="number" step="0.01" min="0" max="5" class="px-3 py-2 rounded-xl border bg-slate-50 w-full"/>
            <div class="text-[11px] text-slate-400 mt-1">0.15 = %15</div>
          </div>
          <div class="md:col-span-5 flex gap-2">
            <button class="px-4 py-2 rounded-xl bg-slate-900 text-white font-extrabold shadow-sm" type="submit">Hesapla</button>
            <a class="px-4 py-2 rounded-xl bg-white border font-extrabold hover:bg-slate-50" href="/app/pricing">Sıfırla</a>
          </div>
        </form>

        <div class="mt-4 grid md:grid-cols-3 gap-2 text-sm">
          <div class="p-3 rounded-xl bg-slate-50 border">
            <div class="text-xs text-slate-500">Fatura Oranı</div>
            <div class="font-extrabold">{fm(invoice_rate)}</div>
          </div>
          <div class="p-3 rounded-xl bg-slate-50 border">
            <div class="text-xs text-slate-500">Tahmini Net (bu fiyatta)</div>
            <div class="font-extrabold">{fm((p - (p*cr) - (p*invoice_rate) - c))}</div>
          </div>
          <div class="p-3 rounded-xl bg-orange-50 border border-orange-200">
            <div class="text-xs text-orange-700">Minimum Kârlı Fiyat</div>
            <div class="font-extrabold text-orange-800">{fm(min_price) if min_price is not None else "Hesaplanamadı"}</div>
          </div>
        </div>

        <div class="mt-3 text-xs text-slate-500">
          Not: Bu ekran simülasyon. Komisyon/indirim ürün ve kampanyaya göre değişir. İstersen gerçek siparişlerden otomatik komisyon oranı çıkarırız.
        </div>
      </div>

      <div class="p-4 rounded-2xl bg-white border shadow-sm">
        <div class="font-extrabold text-lg">Hızlı Kullan</div>
        <div class="mt-3 space-y-2 text-sm">
          <div class="p-3 rounded-xl bg-slate-50 border">
            <div class="font-bold">1) Maliyet gir</div>
            <div class="text-xs text-slate-500">Ürünün maliyeti (alıș + paketleme)</div>
          </div>
          <div class="p-3 rounded-xl bg-slate-50 border">
            <div class="font-bold">2) Komisyon oranı</div>
            <div class="text-xs text-slate-500">Kategori oranına göre</div>
          </div>
          <div class="p-3 rounded-xl bg-slate-50 border">
            <div class="font-bold">3) Hedef kâr</div>
            <div class="text-xs text-slate-500">Örn %15</div>
          </div>
          <div class="p-3 rounded-xl bg-slate-900 text-white">
            <div class="font-extrabold">Çıktı</div>
            <div class="text-xs opacity-80">Minimum kârlı satış fiyatın.</div>
          </div>
        </div>
      </div>
    </div>
    """
    return ui_shell("Fiyat / Hedef", body, active="pricing")


@app.get("/app/returns", response_class=HTMLResponse)
def app_returns(
    start: str = Query(default=""),
    end: str = Query(default=""),
    q: str = Query(default=""),
    auth=Depends(panel_auth)
):
    today = date.today()
    if not start:
        start_dt = datetime.combine(today - timedelta(days=30), datetime.min.time())
    else:
        start_dt = datetime.fromisoformat(start)
    if not end:
        end_dt = datetime.combine(today, datetime.max.time())
    else:
        end_dt = datetime.fromisoformat(end) + timedelta(days=1) - timedelta(milliseconds=1)

    q = (q or "").strip().lower()

    err = ""
    rows = []
    stats = {"lines": 0, "returns": 0, "cancels": 0}
    try:
        orders = fetch_orders(start_ms=_ms(start_dt), end_ms=_ms(end_dt), order_number=None, max_pages=40)
        for o in orders or []:
            order_no = o.get("orderNumber") or ""
            for l in (o.get("lines") or []):
                status_name = (l.get("orderLineItemStatusName") or l.get("orderLineItemStatus") or "").strip()
                status_l = status_name.lower()
                stats["lines"] += 1
                is_return = ("iade" in status_l) or ("return" in status_l)
                is_cancel = ("iptal" in status_l) or ("cancel" in status_l)
                if is_return:
                    stats["returns"] += 1
                if is_cancel:
                    stats["cancels"] += 1
                if not (is_return or is_cancel):
                    continue

                product = l.get("productName") or ""
                sku = l.get("merchantSku") or l.get("sku") or ""
                if q and (q not in product.lower()) and (q not in str(order_no).lower()) and (q not in str(sku).lower()):
                    continue

                c = calc_profit_for_line(l)
                rows.append({
                    "order": order_no,
                    "status": status_name,
                    "product": product,
                    "sku": sku,
                    "sale": c["satis"],
                    "net": c["net_kar"],
                })
        rows = rows[:500]
    except Exception as e:
        err = str(e)

    def tr_money(v: float) -> str:
        try:
            return f"{float(v):,.2f}".replace(",", "X").replace(".", ",").replace("X", ".")
        except Exception:
            return str(v)

    body_rows = ""
    for r in rows:
        badge = "bg-red-50 text-red-700 border-red-200" if ("iade" in (r["status"] or "").lower() or "return" in (r["status"] or "").lower()) else "bg-amber-50 text-amber-700 border-amber-200"
        body_rows += f"""
        <tr class="border-b bg-white">
          <td class="p-2 font-semibold">{r['order']}</td>
          <td class="p-2"><span class="px-2 py-1 rounded-xl border {badge} text-xs font-bold">{r['status']}</span></td>
          <td class="p-2">{r['product']}</td>
          <td class="p-2 text-slate-500">{r['sku']}</td>
          <td class="p-2 text-right">{tr_money(r['sale'])}</td>
          <td class="p-2 text-right font-extrabold">{tr_money(r['net'])}</td>
        </tr>
        """

    body = f"""
    <div class="p-4 rounded-2xl bg-white border shadow-sm">
      <div class="flex flex-wrap gap-3 items-end justify-between">
        <div>
          <div class="font-extrabold text-lg">İadeler / İptaller</div>
          <div class="text-xs text-slate-500">Satır durumundan (orderLineItemStatusName) filtrelenir.</div>
        </div>
        <form class="flex flex-wrap gap-2 items-end" method="get" action="/app/returns">
          <div>
            <div class="text-xs text-slate-500 mb-1">Başlangıç</div>
            <input name="start" type="date" value="{start_dt.date().isoformat()}" class="px-3 py-2 rounded-xl border bg-slate-50"/>
          </div>
          <div>
            <div class="text-xs text-slate-500 mb-1">Bitiş</div>
            <input name="end" type="date" value="{end_dt.date().isoformat()}" class="px-3 py-2 rounded-xl border bg-slate-50"/>
          </div>
          <div>
            <div class="text-xs text-slate-500 mb-1">Ara</div>
            <input name="q" value="{q}" placeholder="sipariş / sku / ürün" class="px-3 py-2 rounded-xl border bg-slate-50 w-56"/>
          </div>
          <button class="px-4 py-2 rounded-xl bg-slate-900 text-white font-extrabold shadow-sm" type="submit">Getir</button>
        </form>
      </div>

      <div class="mt-3 grid md:grid-cols-3 gap-2 text-sm">
        <div class="p-3 rounded-xl bg-slate-50 border">
          <div class="text-xs text-slate-500">Toplam Satır</div>
          <div class="font-extrabold">{stats['lines']}</div>
        </div>
        <div class="p-3 rounded-xl bg-red-50 border border-red-200">
          <div class="text-xs text-red-700">İade</div>
          <div class="font-extrabold text-red-800">{stats['returns']}</div>
        </div>
        <div class="p-3 rounded-xl bg-amber-50 border border-amber-200">
          <div class="text-xs text-amber-700">İptal</div>
          <div class="font-extrabold text-amber-800">{stats['cancels']}</div>
        </div>
      </div>

      {("<div class='mt-3 p-3 rounded-xl bg-red-50 border border-red-200 text-red-700 text-sm'>Hata: "+err+"</div>") if err else ""}

      <div class="mt-4 overflow-auto rounded-xl border">
        <table class="min-w-full text-sm">
          <thead class="bg-slate-100 sticky top-0">
            <tr>
              <th class="text-left p-2">Sipariş</th>
              <th class="text-left p-2">Durum</th>
              <th class="text-left p-2">Ürün</th>
              <th class="text-left p-2">SKU</th>
              <th class="text-right p-2">Satış</th>
              <th class="text-right p-2">Net</th>
            </tr>
          </thead>
          <tbody class="divide-y">
            {body_rows if body_rows else "<tr><td class='p-3 text-slate-500' colspan='6'>Bu aralıkta iade/iptal yok.</td></tr>"}
          </tbody>
        </table>
      </div>
    </div>
    """
    return ui_shell("İadeler", body, active="returns")


@app.get("/app/payouts", response_class=HTMLResponse)
def app_payouts(
    start: str = Query(default=""),
    end: str = Query(default=""),
    auth=Depends(panel_auth)
):
    today = date.today()
    if not start:
        start_dt = datetime.combine(today - timedelta(days=14), datetime.min.time())
    else:
        start_dt = datetime.fromisoformat(start)
    if not end:
        end_dt = datetime.combine(today, datetime.max.time())
    else:
        end_dt = datetime.fromisoformat(end) + timedelta(days=1) - timedelta(milliseconds=1)

    err = ""
    daily = []
    summary = {"sales": 0.0, "comm": 0.0, "disc": 0.0, "inv": 0.0, "net": 0.0, "cost": 0.0, "real_net": 0.0}
    try:
        lines = _try_fetch_lines(start_dt, end_dt, max_pages=40)
        by_day = {}
        for x in lines:
            od = x.get("orderDate")
            d = (od.date().isoformat() if hasattr(od, "date") and od else "unknown")
            a = by_day.setdefault(d, {"day": d, "sales": 0.0, "comm": 0.0, "disc": 0.0, "inv": 0.0, "net": 0.0, "cost": 0.0, "real_net": 0.0})
            q = float(x.get("qty", 1) or 1)
            cost = float(x.get("unit_cost", 0.0)) * q
            a["sales"] += x.get("satis", 0.0)
            a["comm"] += x.get("komisyon", 0.0)
            a["disc"] += x.get("satici_indirim", 0.0)
            a["inv"] += x.get("fatura", 0.0)
            a["net"] += x.get("net_kar", 0.0)
            a["cost"] += cost
            a["real_net"] += x.get("net_kar", 0.0) - cost

        daily = sorted(by_day.values(), key=lambda r: r["day"])
        for r in daily:
            summary["sales"] += r["sales"]
            summary["comm"] += r["comm"]
            summary["disc"] += r["disc"]
            summary["inv"] += r["inv"]
            summary["net"] += r["net"]
            summary["cost"] += r["cost"]
            summary["real_net"] += r["real_net"]
    except Exception as e:
        err = str(e)

    def tr_money(v: float) -> str:
        try:
            return f"{float(v):,.2f}".replace(",", "X").replace(".", ",").replace("X", ".")
        except Exception:
            return str(v)

    rows = ""
    for r in daily:
        rows += f"""
        <tr class="border-b bg-white">
          <td class="p-2 font-semibold whitespace-nowrap">{r['day']}</td>
          <td class="p-2 text-right">{tr_money(r['sales'])}</td>
          <td class="p-2 text-right">{tr_money(r['comm'])}</td>
          <td class="p-2 text-right">{tr_money(r['disc'])}</td>
          <td class="p-2 text-right">{tr_money(r['inv'])}</td>
          <td class="p-2 text-right">{tr_money(r['cost'])}</td>
          <td class="p-2 text-right font-extrabold">{tr_money(r['real_net'])}</td>
        </tr>
        """

    body = f"""
    <div class="grid lg:grid-cols-3 gap-3">
      <div class="lg:col-span-2 p-4 rounded-2xl bg-white border shadow-sm">
        <div class="flex flex-wrap gap-3 items-end justify-between">
          <div>
            <div class="font-extrabold text-lg">Hakediş (Tahmini)</div>
            <div class="text-xs text-slate-500">Gün gün: satış → kesintiler → tahmini gerçek net.</div>
          </div>
          <form class="flex flex-wrap gap-2 items-end" method="get" action="/app/payouts">
            <div>
              <div class="text-xs text-slate-500 mb-1">Başlangıç</div>
              <input name="start" type="date" value="{start_dt.date().isoformat()}" class="px-3 py-2 rounded-xl border bg-slate-50"/>
            </div>
            <div>
              <div class="text-xs text-slate-500 mb-1">Bitiş</div>
              <input name="end" type="date" value="{end_dt.date().isoformat()}" class="px-3 py-2 rounded-xl border bg-slate-50"/>
            </div>
            <button class="px-4 py-2 rounded-xl bg-slate-900 text-white font-extrabold shadow-sm" type="submit">Getir</button>
          </form>
        </div>

        {("<div class='mt-3 p-3 rounded-xl bg-red-50 border border-red-200 text-red-700 text-sm'>Hata: "+err+"</div>") if err else ""}

        <div class="mt-4 overflow-auto rounded-xl border">
          <table class="min-w-full text-sm">
            <thead class="bg-slate-100 sticky top-0">
              <tr>
                <th class="text-left p-2">Gün</th>
                <th class="text-right p-2">Satış</th>
                <th class="text-right p-2">Komisyon</th>
                <th class="text-right p-2">İndirim</th>
                <th class="text-right p-2">Fatura</th>
                <th class="text-right p-2">Maliyet</th>
                <th class="text-right p-2">Tahmini Gerçek Net</th>
              </tr>
            </thead>
            <tbody class="divide-y">
              {rows if rows else "<tr><td class='p-3 text-slate-500' colspan='7'>Kayıt yok.</td></tr>"}
            </tbody>
          </table>
        </div>

        <div class="mt-3 text-xs text-slate-500">
          Not: Trendyol'un gerçek hakediş ekranındaki kesintiler farklı kalemler içerebilir. Bu tablo “yönetim” amaçlı tahmindir.
        </div>
      </div>

      <div class="p-4 rounded-2xl bg-white border shadow-sm">
        <div class="font-extrabold text-lg">Toplam</div>
        <div class="mt-3 space-y-2 text-sm">
          <div class="p-3 rounded-xl bg-slate-50 border flex justify-between"><span>Satış</span><b>{tr_money(summary['sales'])}</b></div>
          <div class="p-3 rounded-xl bg-slate-50 border flex justify-between"><span>Komisyon</span><b>{tr_money(summary['comm'])}</b></div>
          <div class="p-3 rounded-xl bg-slate-50 border flex justify-between"><span>İndirim</span><b>{tr_money(summary['disc'])}</b></div>
          <div class="p-3 rounded-xl bg-slate-50 border flex justify-between"><span>Fatura</span><b>{tr_money(summary['inv'])}</b></div>
          <div class="p-3 rounded-xl bg-slate-50 border flex justify-between"><span>Maliyet</span><b>{tr_money(summary['cost'])}</b></div>
          <div class="p-3 rounded-xl bg-orange-50 border border-orange-200 flex justify-between"><span class="text-orange-700">Tahmini Gerçek Net</span><b class="text-orange-800">{tr_money(summary['real_net'])}</b></div>
        </div>
      </div>
    </div>
    """
    return ui_shell("Hakediş", body, active="payouts")


@app.get("/app/campaigns", response_class=HTMLResponse)
def app_campaigns(
    start: str = Query(default=""),
    end: str = Query(default=""),
    auth=Depends(panel_auth)
):
    today = date.today()
    if not start:
        start_dt = datetime.combine(today - timedelta(days=30), datetime.min.time())
    else:
        start_dt = datetime.fromisoformat(start)
    if not end:
        end_dt = datetime.combine(today, datetime.max.time())
    else:
        end_dt = datetime.fromisoformat(end) + timedelta(days=1) - timedelta(milliseconds=1)

    err = ""
    rows = []
    try:
        lines = _try_fetch_lines(start_dt, end_dt, max_pages=40)
        agg = {}
        for x in lines:
            camp = x.get("campaign") or "0"
            key = str(camp)
            a = agg.setdefault(key, {"campaign": key, "qty": 0, "sales": 0.0, "comm": 0.0, "disc": 0.0, "inv": 0.0, "net": 0.0, "cost": 0.0, "real_net": 0.0})
            q = float(x.get("qty", 1) or 1)
            a["qty"] += int(q)
            a["sales"] += x.get("satis", 0.0)
            a["comm"] += x.get("komisyon", 0.0)
            a["disc"] += x.get("satici_indirim", 0.0)
            a["inv"] += x.get("fatura", 0.0)
            a["net"] += x.get("net_kar", 0.0)
            cost = float(x.get("unit_cost", 0.0)) * q
            a["cost"] += cost
            a["real_net"] += x.get("net_kar", 0.0) - cost
        rows = sorted(agg.values(), key=lambda r: r.get("real_net", 0.0))
    except Exception as e:
        err = str(e)

    def tr_money(v: float) -> str:
        try:
            return f"{float(v):,.2f}".replace(",", "X").replace(".", ",").replace("X", ".")
        except Exception:
            return str(v)

    body_rows = ""
    for r in rows:
        badge = "bg-red-50 text-red-700 border-red-200" if r["real_net"] < 0 else "bg-emerald-50 text-emerald-700 border-emerald-200"
        body_rows += f"""
        <tr class="border-b bg-white">
          <td class="p-2 font-semibold">#{r['campaign']}</td>
          <td class="p-2 text-right">{r['qty']}</td>
          <td class="p-2 text-right">{tr_money(r['sales'])}</td>
          <td class="p-2 text-right">{tr_money(r['comm'])}</td>
          <td class="p-2 text-right">{tr_money(r['disc'])}</td>
          <td class="p-2 text-right">{tr_money(r['inv'])}</td>
          <td class="p-2 text-right">{tr_money(r['cost'])}</td>
          <td class="p-2 text-right font-extrabold"><span class="px-2 py-1 rounded-xl border {badge}">{tr_money(r['real_net'])}</span></td>
        </tr>
        """

    body = f"""
    <div class="p-4 rounded-2xl bg-white border shadow-sm">
      <div class="flex flex-wrap gap-3 items-end justify-between">
        <div>
          <div class="font-extrabold text-lg">Kampanyalar / İndirim Etkisi</div>
          <div class="text-xs text-slate-500">Sipariş satırlarından <b>salesCampaignId</b> ile kampanya kârlılığı.</div>
        </div>
        <form class="flex flex-wrap gap-2 items-end" method="get" action="/app/campaigns">
          <div>
            <div class="text-xs text-slate-500 mb-1">Başlangıç</div>
            <input name="start" type="date" value="{start_dt.date().isoformat()}" class="px-3 py-2 rounded-xl border bg-slate-50"/>
          </div>
          <div>
            <div class="text-xs text-slate-500 mb-1">Bitiş</div>
            <input name="end" type="date" value="{end_dt.date().isoformat()}" class="px-3 py-2 rounded-xl border bg-slate-50"/>
          </div>
          <button class="px-4 py-2 rounded-xl bg-slate-900 text-white font-extrabold shadow-sm" type="submit">Getir</button>
        </form>
      </div>

      {("<div class='mt-3 p-3 rounded-xl bg-red-50 border border-red-200 text-red-700 text-sm'>Hata: "+err+"</div>") if err else ""}

      <div class="mt-4 overflow-auto rounded-xl border">
        <table class="min-w-full text-sm">
          <thead class="bg-slate-100 sticky top-0">
            <tr>
              <th class="text-left p-2">Kampanya</th>
              <th class="text-right p-2">Adet</th>
              <th class="text-right p-2">Satış</th>
              <th class="text-right p-2">Komisyon</th>
              <th class="text-right p-2">İndirim</th>
              <th class="text-right p-2">Fatura</th>
              <th class="text-right p-2">Maliyet</th>
              <th class="text-right p-2">Gerçek Net</th>
            </tr>
          </thead>
          <tbody class="divide-y">
            {body_rows if body_rows else "<tr><td class='p-3 text-slate-500' colspan='8'>Kayıt yok.</td></tr>"}
          </tbody>
        </table>
      </div>

      <div class="mt-3 text-xs text-slate-500">
        İpucu: Gerçek Net <b>eksi</b> olan kampanyalarda fiyat/indirim/komisyonu gözden geçir.
      </div>
    </div>
    """
    return ui_shell("Kampanyalar", body, active="campaigns")


@app.get("/app/costs", response_class=HTMLResponse)
def app_costs(auth=Depends(panel_auth)):
    costs = []
    err = ""
    try:
        conn = db()
        cur = conn.cursor()
        cur.execute("SELECT merchant_sku, cost, updated_at FROM sku_costs ORDER BY merchant_sku")
        costs = cur.fetchall()
    except Exception as e:
        err = str(e)

    rows = ""
    for sku, cost, upd in (costs or []):
        rows += f"""
        <tr class="border-b bg-white">
          <td class="p-2 font-semibold">{sku}</td>
          <td class="p-2 text-right">{float(cost):.2f}</td>
          <td class="p-2 text-slate-500 text-xs">{upd or ""}</td>
          <td class="p-2">
            <form method="post" action="/costs/delete" onsubmit="return confirm('Silinsin mi?');">
              <input type="hidden" name="merchant_sku" value="{sku}"/>
              <button class="px-3 py-1.5 rounded-xl bg-white border hover:bg-slate-50 font-bold" type="submit">Sil</button>
            </form>
          </td>
        </tr>
        """

    body = f"""
    <div class="grid lg:grid-cols-3 gap-3">
      <div class="lg:col-span-1 p-4 rounded-2xl bg-white border shadow-sm">
        <div class="font-extrabold text-lg">SKU Maliyet Ekle</div>
        <div class="text-xs text-slate-500">Melontik gibi “gerçek net” için şart. SKU’ya birim maliyet gir.</div>

        <form class="mt-4 space-y-2" method="post" action="/costs/upsert">
          <div>
            <div class="text-xs text-slate-500 mb-1">Merchant SKU</div>
            <input name="merchant_sku" required placeholder="örn: merchantSku" class="px-3 py-2 rounded-xl border bg-slate-50 w-full"/>
          </div>
          <div>
            <div class="text-xs text-slate-500 mb-1">Birim Maliyet (TRY)</div>
            <input name="cost" required type="number" step="0.01" min="0" class="px-3 py-2 rounded-xl border bg-slate-50 w-full"/>
          </div>
          <button class="w-full px-4 py-2 rounded-xl bg-orange-500 text-white font-extrabold shadow-sm" type="submit">Kaydet / Güncelle</button>
        </form>

        <div class="mt-4 p-3 rounded-xl bg-slate-900 text-white text-xs">
          <div class="font-extrabold">Not</div>
          <div class="opacity-80">Kârlılık ekranında “Gerçek Net” otomatik hesaplanır. Fiyat/Hedef ekranında SKU yazarsan maliyeti otomatik çeker.</div>
        </div>
      </div>

      <div class="lg:col-span-2 p-4 rounded-2xl bg-white border shadow-sm">
        <div class="flex items-end justify-between gap-2">
          <div>
            <div class="font-extrabold text-lg">Maliyet Listesi</div>
            <div class="text-xs text-slate-500">Toplam: {len(costs or [])} SKU</div>
          </div>
          <a class="px-4 py-2 rounded-xl bg-white border font-extrabold hover:bg-slate-50" href="/app/profit">Kârlılığa Git</a>
        </div>

        {("<div class='mt-3 p-3 rounded-xl bg-red-50 border border-red-200 text-red-700 text-sm'>Hata: "+err+"</div>") if err else ""}

        <div class="mt-4 overflow-auto rounded-xl border">
          <table class="min-w-full text-sm">
            <thead class="bg-slate-100 sticky top-0">
              <tr>
                <th class="text-left p-2">SKU</th>
                <th class="text-right p-2">Maliyet</th>
                <th class="text-left p-2">Güncelleme</th>
                <th class="text-left p-2">İşlem</th>
              </tr>
            </thead>
            <tbody class="divide-y">
              {rows if rows else "<tr><td class='p-3 text-slate-500' colspan='4'>Henüz maliyet yok.</td></tr>"}
            </tbody>
          </table>
        </div>
      </div>
    </div>
    """
    return ui_shell("Maliyetler", body, active="profit")


@app.post("/costs/upsert")
def costs_upsert(merchant_sku: str = Form(...), cost: float = Form(...), auth=Depends(panel_auth)):
    upsert_cost(merchant_sku, float(cost))
    return RedirectResponse(url="/app/costs", status_code=303)


@app.post("/costs/delete")
def costs_delete(merchant_sku: str = Form(...), auth=Depends(panel_auth)):
    delete_cost(merchant_sku)
    return RedirectResponse(url="/app/costs", status_code=303)

# =========================
# FATURA API
# =========================
@app.post("/invoice/draft")
def invoice_draft(orderNumber: str = Form(...), auth=Depends(panel_auth)):
    orderNumber = str(orderNumber).strip()
    if not orderNumber:
        raise HTTPException(400, "orderNumber boş olamaz")

    # ✅ varsa önce DB'den dön (taslağı çoğaltma)
    existing = get_existing_invoice_id_by_order(orderNumber)
    if existing:
        return RedirectResponse(url="/app/invoices", status_code=303)

    # ✅ Trendyol’dan sağlam bul
    o = find_order_by_number(orderNumber)
    if not o:
        raise HTTPException(404, f"Sipariş bulunamadı: {orderNumber}. Debug: /debug/find-order?orderNumber={orderNumber}")

    _ = create_invoice_draft_from_order(o)
    return RedirectResponse(url="/app/invoices", status_code=303)

@app.get("/invoice/{invoice_id}")
def invoice_get(invoice_id: int, auth=Depends(panel_auth)):
    return get_invoice(invoice_id)

@app.get("/invoice/{invoice_id}/xml")
def invoice_xml(invoice_id: int, auth=Depends(panel_auth)):
    data = get_invoice(invoice_id)
    inv = data["invoice"]
    lines = data["lines"]
    xml_bytes = build_basic_ubl_xml(inv, lines)

    tmp = tempfile.NamedTemporaryFile(delete=False, suffix=".xml")
    with open(tmp.name, "wb") as f:
        f.write(xml_bytes)

    filename = f"earshiv_{inv['order_number']}_{inv['invoice_uuid']}.xml"
    return FileResponse(tmp.name, filename=filename, media_type="application/xml")

@app.get("/invoice/{invoice_id}/pdf")
def invoice_pdf(invoice_id: int, auth=Depends(panel_auth)):
    data = get_invoice(invoice_id)
    inv = data["invoice"]
    lines = data["lines"]
    pdf_path = build_pdf(inv, lines)
    filename = f"earshiv_{inv['order_number']}_{inv['invoice_uuid']}.pdf"
    return FileResponse(pdf_path, filename=filename, media_type="application/pdf")
