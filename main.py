from fastapi import FastAPI, Depends, HTTPException, status, Query, Form
from fastapi.security import HTTPBasic, HTTPBasicCredentials
from fastapi.responses import FileResponse, HTMLResponse, RedirectResponse
import os, base64, requests, tempfile, sqlite3, uuid
from datetime import datetime, date, timedelta
from openpyxl import Workbook
from reportlab.lib.pagesizes import A4
from reportlab.pdfgen import canvas
from typing import Optional
from xml.etree.ElementTree import Element, SubElement, tostring

app = FastAPI(title="Trendyol Kar/Zarar + e-Arşiv Taslak (Sağlam)")

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
def ui_shell(title: str, body: str) -> str:
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
  <div class="max-w-6xl mx-auto p-4">
    <div class="flex items-center justify-between py-4">
      <div class="flex items-center gap-3">
        <div class="w-10 h-10 rounded-xl bg-orange-500"></div>
        <div>
          <div class="font-extrabold text-lg">Trendyol Panel</div>
          <div class="text-xs text-slate-500">Kar/Zarar + e-Arşiv Taslak</div>
        </div>
      </div>
      <div class="flex gap-2">
        <a class="px-3 py-2 rounded-xl bg-white shadow-sm border hover:bg-slate-100" href="/app">Dashboard</a>
        <a class="px-3 py-2 rounded-xl bg-white shadow-sm border hover:bg-slate-100" href="/app/invoices">Faturalar</a>
      </div>
    </div>
    {body}
    <div class="text-xs text-slate-400 py-8">© {date.today().year}</div>
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

    body = f"""
    <div class="grid md:grid-cols-4 gap-3">
      <div class="p-4 rounded-2xl bg-white border shadow-sm">
        <div class="text-xs text-slate-500">Sipariş</div>
        <div class="text-2xl font-extrabold" id="k1">-</div>
      </div>
      <div class="p-4 rounded-2xl bg-white border shadow-sm">
        <div class="text-xs text-slate-500">Satış</div>
        <div class="text-2xl font-extrabold" id="k2">-</div>
      </div>
      <div class="p-4 rounded-2xl bg-white border shadow-sm">
        <div class="text-xs text-slate-500">Toplam Kesinti</div>
        <div class="text-2xl font-extrabold" id="k3">-</div>
      </div>
      <div class="p-4 rounded-2xl bg-white border shadow-sm">
        <div class="text-xs text-slate-500">Net Kâr</div>
        <div class="text-2xl font-extrabold" id="k4">-</div>
      </div>
    </div>

    <div class="mt-4 p-4 rounded-2xl bg-white border shadow-sm">
      <div class="flex flex-wrap gap-3 items-end">
        <div>
          <div class="text-xs text-slate-500 mb-1">Başlangıç</div>
          <input id="start" type="date" value="{week_ago.isoformat()}" class="px-3 py-2 rounded-xl border bg-slate-50"/>
        </div>
        <div>
          <div class="text-xs text-slate-500 mb-1">Bitiş</div>
          <input id="end" type="date" value="{today.isoformat()}" class="px-3 py-2 rounded-xl border bg-slate-50"/>
        </div>
        <button onclick="loadAll()" class="px-4 py-2 rounded-xl bg-orange-500 text-white font-bold">Getir</button>
        <a id="excel" class="px-4 py-2 rounded-xl bg-slate-900 text-white font-bold" href="#">Excel</a>
      </div>

      <div class="mt-4 overflow-auto">
        <table class="min-w-full text-sm">
          <thead class="bg-slate-100">
            <tr>
              <th class="text-left p-2">Sipariş</th>
              <th class="text-left p-2">Ürün</th>
              <th class="text-left p-2">Kampanya</th>
              <th class="text-right p-2">Satış</th>
              <th class="text-right p-2">Komisyon</th>
              <th class="text-right p-2">Satıcı İnd.</th>
              <th class="text-right p-2">Fatura %10</th>
              <th class="text-right p-2">Net</th>
              <th class="text-left p-2">Fatura</th>
            </tr>
          </thead>
          <tbody id="tb" class="divide-y">
            <tr><td class="p-2 text-slate-500" colspan="9">Tarih seçip Getir'e bas.</td></tr>
          </tbody>
        </table>
      </div>
    </div>

<script>
function money(x){{
  const n = Number(x||0);
  return n.toLocaleString('tr-TR',{{minimumFractionDigits:2, maximumFractionDigits:2}});
}}
async function loadAll(){{
  const s = document.getElementById('start').value;
  const e = document.getElementById('end').value;
  document.getElementById('excel').href = `/report/excel?start=${{encodeURIComponent(s)}}&end=${{encodeURIComponent(e)}}`;

  const r1 = await fetch(`/report?start=${{encodeURIComponent(s)}}&end=${{encodeURIComponent(e)}}`);
  const sum = await r1.json();
  document.getElementById('k1').innerText = sum.siparis ?? '-';
  document.getElementById('k2').innerText = money(sum.satis_toplam);
  document.getElementById('k3').innerText = money(sum.toplam_kesinti_toplam);
  document.getElementById('k4').innerText = money(sum.net_kar_toplam);

  const r2 = await fetch(`/report/lines?start=${{encodeURIComponent(s)}}&end=${{encodeURIComponent(e)}}`);
  const det = await r2.json();
  const tb = document.getElementById('tb');
  tb.innerHTML = '';
  (det.rows||[]).forEach(row=>{{
    const tr = document.createElement('tr');
    const orderNo = row['Sipariş'] || '';
    tr.innerHTML = `
      <td class="p-2">${{orderNo}}</td>
      <td class="p-2">${{row['Ürün']||''}}</td>
      <td class="p-2">${{row['Kampanya']||''}}</td>
      <td class="p-2 text-right">${{money(row['Satış'])}}</td>
      <td class="p-2 text-right">${{money(row['Komisyon'])}}</td>
      <td class="p-2 text-right">${{money(row['Satıcı İndirim'])}}</td>
      <td class="p-2 text-right">${{money(row['Fatura %10'])}}</td>
      <td class="p-2 text-right font-bold">${{money(row['Net Kâr'])}}</td>
      <td class="p-2">
        <form method="post" action="/invoice/draft">
          <input type="hidden" name="orderNumber" value="${{orderNo}}"/>
          <button class="px-3 py-1 rounded-lg bg-white border hover:bg-slate-50" type="submit">Taslak</button>
        </form>
      </td>
    `;
    tb.appendChild(tr);
  }});
}}
</script>
"""
    return ui_shell("Dashboard", body)

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
    return ui_shell("Faturalar", body)

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
