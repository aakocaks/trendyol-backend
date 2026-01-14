# main.py
from fastapi import FastAPI, Depends, HTTPException, status, Query
from fastapi.security import HTTPBasic, HTTPBasicCredentials
from fastapi.responses import FileResponse, StreamingResponse, HTMLResponse
import os, base64, requests, tempfile
from datetime import datetime, date, timedelta
from openpyxl import Workbook
import pandas as pd
from io import BytesIO
from typing import Any

app = FastAPI(title="Trendyol Kar/Zarar Paneli")
security = HTTPBasic()

# =================================================
# AYARLAR
# =================================================
INVOICE_RATE = float(os.getenv("INVOICE_RATE", "0.10"))  # senin %10 fatura
PAGE_SIZE = int(os.getenv("TRENDYOL_PAGE_SIZE", "200"))

# =================================================
# PANEL AUTH
# =================================================
def panel_auth(credentials: HTTPBasicCredentials = Depends(security)):
    user = os.getenv("PANEL_USER")
    password = os.getenv("PANEL_PASS")

    if not user or not password:
        raise HTTPException(
            status_code=status.HTTP_500_INTERNAL_SERVER_ERROR,
            detail="PANEL_USER / PANEL_PASS env eksik",
        )

    if credentials.username != user or credentials.password != password:
        raise HTTPException(
            status_code=status.HTTP_401_UNAUTHORIZED,
            detail="Yetkisiz",
            headers={"WWW-Authenticate": "Basic"},
        )

# =================================================
# HELPERS
# =================================================
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
    # sende price/amount/lineGrossAmount/lineUnitPrice var
    price = pick(line, ["price", "amount", "lineGrossAmount", "totalPrice", "totalAmount"], default=0.0)
    if price and price > 0:
        return price
    unit = pick(line, ["lineUnitPrice", "unitPrice", "unitSalePrice", "sellingPrice"], default=0.0)
    return unit * get_qty(line)

def get_commission(line: dict) -> float:
    return pick(line, ["commission", "commissionAmount", "tyCommissionAmount", "commissionTotal"], default=0.0)

# =================================================
# İNDİRİM / KAMPANYA (SENİN GELEN ALANLARA GÖRE)
# =================================================
def parse_discounts(line: dict) -> tuple[float, float]:
    """
    (seller_discount, trendyol_discount)

    Senin sample_line’da şu alanlar var:
      - lineSellerDiscount
      - lineTyDiscount
      - lineTotalDiscount
      - discountDetails[*].lineItemSellerDiscount / lineItemTyDiscount
      - tyDiscount (bazı yerlerde)
    """
    # 1) direkt line alanları
    seller = pick(line, ["lineSellerDiscount", "sellerDiscountAmount", "sellerDiscount", "sellerDiscountTotal"], default=0.0)
    ty = pick(line, ["lineTyDiscount", "tyDiscount", "tyDiscountAmount", "trendyolDiscountAmount", "marketplaceDiscountAmount"], default=0.0)

    # 2) discountDetails listesi (sende var)
    details = line.get("discountDetails")
    if isinstance(details, list):
        for obj in details:
            if not isinstance(obj, dict):
                continue
            seller += pick(obj, ["lineItemSellerDiscount", "sellerDiscountAmount", "sellerDiscount"], default=0.0)
            ty += pick(obj, ["lineItemTyDiscount", "tyDiscountAmount", "trendyolDiscountAmount"], default=0.0)

    # 3) total discount var ama owner ayrımı yoksa dokunmuyoruz (çifte saymasın diye)
    # total = pick(line, ["lineTotalDiscount", "discount"], default=0.0)

    return float(seller), float(ty)

def get_campaign_label(line: dict) -> str:
    """
    Sende kampanya: salesCampaignId geliyor.
    """
    scid = line.get("salesCampaignId")
    if scid is not None and str(scid).strip():
        return f"salesCampaignId:{scid}"
    # fallback varsa
    for k in ["campaignName", "promotionName", "flashSaleName", "campaign", "promotion", "discountName"]:
        v = line.get(k)
        if isinstance(v, str) and v.strip():
            return v.strip()
    return ""

# =================================================
# KARGO (SİPARİŞ SEVİYESİNDEN YAKALA + SATIRLARA PAYLAŞTIR)
# =================================================
def get_order_cargo_total(order: dict) -> float:
    """
    Kargo bazı hesaplarda order içinde, bazı hesaplarda shipmentPackages içinde gelir.
    Biz olabildiğince yakalıyoruz.
    """
    # order level dene
    cargo = pick(order, ["cargoPrice", "cargoAmount", "shipmentFee", "shippingFee", "sellerCargoAmount"], default=0.0)
    if cargo and cargo > 0:
        return cargo

    # shipmentPackages varsa tara
    packs = order.get("shipmentPackages")
    if isinstance(packs, list):
        total = 0.0
        for p in packs:
            if not isinstance(p, dict):
                continue
            total += pick(p, ["cargoPrice", "cargoAmount", "shipmentFee", "shippingFee", "sellerCargoAmount"], default=0.0)
        return float(total)

    return 0.0

def allocate_cargo_per_line(order: dict) -> dict[int, float]:
    """
    Kargo toplamını satır satış tutarına göre paylaştırır.
    Dönüş: {lineId/int: allocated_cargo}
    """
    total_cargo = get_order_cargo_total(order)
    lines = order.get("lines") or []
    if not total_cargo or not lines:
        return {}

    # ağırlık: satış fiyatı (line)
    weights = []
    sum_w = 0.0
    for l in lines:
        if not isinstance(l, dict):
            continue
        w = get_sale_price(l)
        if w < 0:
            w = 0.0
        weights.append((l, w))
        sum_w += w

    if sum_w <= 0:
        # eşit böl
        per = float(total_cargo) / float(len(weights))
        out = {}
        for l, _ in weights:
            lid = l.get("lineId") or l.get("id")
            if lid is not None:
                out[int(lid)] = per
        return out

    out = {}
    # paylaştır
    for l, w in weights:
        lid = l.get("lineId") or l.get("id")
        if lid is None:
            continue
        out[int(lid)] = float(total_cargo) * (w / sum_w)
    return out

# =================================================
# TEK HESAP (ORDER CONTEXT + LINE)
# =================================================
def calc_profit_for_line(line: dict, allocated_cargo: float = 0.0) -> dict:
    qty = get_qty(line)
    sale = get_sale_price(line)

    commission = get_commission(line)
    seller_disc, ty_disc = parse_discounts(line)

    # kargo: satırdan gelmiyor → siparişten paylaştırılmış değer
    cargo = float(allocated_cargo or 0.0)

    # Fatura %10: (satış - satıcı indirimi) üzerinden
    invoice_base = max(sale - seller_disc, 0.0)
    invoice = invoice_base * INVOICE_RATE

    total_deductions = commission + cargo + seller_disc + invoice
    net_profit = sale - total_deductions  # trendyol indirimini düşmüyoruz (gösteriyoruz)

    return {
        "kampanya": get_campaign_label(line),
        "adet": qty,
        "satis": round(sale, 2),
        "komisyon": round(commission, 2),
        "kargo": round(cargo, 2),
        "satici_indirim": round(seller_disc, 2),
        "trendyol_indirim": round(ty_disc, 2),
        f"fatura_%{int(INVOICE_RATE*100)}": round(invoice, 2),
        "toplam_kesinti": round(total_deductions, 2),
        "net_kar": round(net_profit, 2),
    }

# =================================================
# TRENDYOL API
# =================================================
def trendyol_headers() -> tuple[str, dict]:
    api_key = os.getenv("TRENDYOL_API_KEY")
    api_secret = os.getenv("TRENDYOL_API_SECRET")
    seller_id = os.getenv("TRENDYOL_SELLER_ID")

    if not api_key or not api_secret or not seller_id:
        raise HTTPException(
            status_code=500,
            detail="TRENDYOL_API_KEY / TRENDYOL_API_SECRET / TRENDYOL_SELLER_ID env eksik",
        )

    auth = base64.b64encode(f"{api_key}:{api_secret}".encode()).decode()
    url = f"https://api.trendyol.com/sapigw/suppliers/{seller_id}/orders"
    headers = {
        "Authorization": f"Basic {auth}",
        "User-Agent": f"{seller_id} - Trendyol API",
    }
    return url, headers

def fetch_orders(start_ms: int | None = None, end_ms: int | None = None) -> list[dict]:
    url, headers = trendyol_headers()
    orders: list[dict] = []

    page = 0
    while True:
        params = {"page": page, "size": PAGE_SIZE}
        if start_ms is not None:
            params["startDate"] = start_ms
        if end_ms is not None:
            params["endDate"] = end_ms

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
        if page > 200:
            break

    return orders

# =================================================
# ENDPOINTS - KONTROL
# =================================================
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
    }

# =================================================
# DEBUG (SENİN GELEN ALANLARI GÖRMEK İÇİN)
# =================================================
@app.get("/debug/line-sample")
def debug_line_sample(
    start: str = Query(..., description="YYYY-MM-DD"),
    end: str = Query(..., description="YYYY-MM-DD"),
):
    start_ms, end_ms = date_range_to_ms(start, end)
    orders = fetch_orders(start_ms=start_ms, end_ms=end_ms)

    for o in orders:
        lines = o.get("lines") or []
        if lines:
            cargo_map = allocate_cargo_per_line(o)
            sample = lines[0]
            lid = sample.get("lineId") or sample.get("id")
            allocated = cargo_map.get(int(lid), 0.0) if lid is not None else 0.0
            calc = calc_profit_for_line(sample, allocated_cargo=allocated)
            return {
                "orderNumber": o.get("orderNumber"),
                "order_keys": sorted(list(o.keys())),
                "order_cargo_total": get_order_cargo_total(o),
                "sample_line_keys": sorted(list(sample.keys())),
                "sample_line": sample,
                "allocated_cargo_for_sample": round(float(allocated), 2),
                "calculated": calc,
            }

    return {"message": "Bu tarih aralığında sample bulunamadı."}

# =================================================
# REPORT (JSON)
# =================================================
@app.get("/report")
def report(
    start: str = Query(..., description="YYYY-MM-DD"),
    end: str = Query(..., description="YYYY-MM-DD"),
):
    start_ms, end_ms = date_range_to_ms(start, end)
    orders = fetch_orders(start_ms=start_ms, end_ms=end_ms)

    toplam_siparis = 0
    toplam_satis = 0.0
    toplam_komisyon = 0.0
    toplam_kargo = 0.0
    toplam_satici_indirim = 0.0
    toplam_trendyol_indirim = 0.0
    toplam_fatura = 0.0
    toplam_net = 0.0

    for o in orders:
        od = o.get("orderDate")
        if isinstance(od, int) and not (start_ms <= od <= end_ms):
            continue

        toplam_siparis += 1
        cargo_map = allocate_cargo_per_line(o)

        for l in (o.get("lines") or []):
            lid = l.get("lineId") or l.get("id")
            allocated = cargo_map.get(int(lid), 0.0) if lid is not None else 0.0

            calc = calc_profit_for_line(l, allocated_cargo=allocated)
            toplam_satis += calc["satis"]
            toplam_komisyon += calc["komisyon"]
            toplam_kargo += calc["kargo"]
            toplam_satici_indirim += calc["satici_indirim"]
            toplam_trendyol_indirim += calc["trendyol_indirim"]
            toplam_fatura += calc.get(f"fatura_%{int(INVOICE_RATE*100)}", 0.0)
            toplam_net += calc["net_kar"]

    return {
        "tarih": {"start": start, "end": end},
        "siparis": int(toplam_siparis),
        "satis_toplam": round(toplam_satis, 2),
        "komisyon_toplam": round(toplam_komisyon, 2),
        "kargo_toplam": round(toplam_kargo, 2),
        "satici_indirim_toplam": round(toplam_satici_indirim, 2),
        "trendyol_indirim_toplam": round(toplam_trendyol_indirim, 2),
        f"fatura_%{int(INVOICE_RATE*100)}_toplam": round(toplam_fatura, 2),
        "net_kar_toplam": round(toplam_net, 2),
    }

@app.get("/report/lines")
def report_lines(
    start: str = Query(..., description="YYYY-MM-DD"),
    end: str = Query(..., description="YYYY-MM-DD"),
):
    start_ms, end_ms = date_range_to_ms(start, end)
    orders = fetch_orders(start_ms=start_ms, end_ms=end_ms)

    rows = []
    for o in orders:
        od = o.get("orderDate")
        if isinstance(od, int) and not (start_ms <= od <= end_ms):
            continue

        cargo_map = allocate_cargo_per_line(o)
        order_no = o.get("orderNumber") or o.get("id") or ""
        for l in (o.get("lines") or []):
            lid = l.get("lineId") or l.get("id")
            allocated = cargo_map.get(int(lid), 0.0) if lid is not None else 0.0

            calc = calc_profit_for_line(l, allocated_cargo=allocated)
            rows.append({
                "Sipariş": order_no,
                "Ürün": l.get("productName") or l.get("name") or "",
                "Barkod/SKU": l.get("barcode") or l.get("merchantSku") or "",
                "Kampanya": calc["kampanya"],
                "Satış": calc["satis"],
                "Komisyon": calc["komisyon"],
                "Kargo": calc["kargo"],
                "Satıcı İndirim": calc["satici_indirim"],
                "Trendyol İndirim": calc["trendyol_indirim"],
                f"Fatura %{int(INVOICE_RATE*100)}": calc.get(f"fatura_%{int(INVOICE_RATE*100)}", 0.0),
                "Net Kâr": calc["net_kar"],
            })

    return {"tarih": {"start": start, "end": end}, "adet": len(rows), "rows": rows}

# =================================================
# EXCEL
# =================================================
@app.get("/report/excel")
def report_excel(
    start: str = Query(..., description="YYYY-MM-DD"),
    end: str = Query(..., description="YYYY-MM-DD"),
):
    data = report_lines(start, end)
    rows = data["rows"]
    sumdata = report(start, end)

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

# =================================================
# PANEL
# =================================================
@app.get("/panel", response_class=HTMLResponse)
def panel(auth=Depends(panel_auth)):
    today = date.today()
    week_ago = today - timedelta(days=6)

    return f"""
<!DOCTYPE html>
<html lang="tr">
<head>
  <meta charset="UTF-8">
  <title>Trendyol Kar/Zarar Panel</title>
  <meta name="viewport" content="width=device-width, initial-scale=1">
  <style>
    body {{ margin:0; font-family:Arial,sans-serif; background:#f6f7fb; color:#111; }}
    .wrap {{ max-width:1200px; margin:28px auto; padding:0 16px; }}
    .card {{ background:#fff; border-radius:14px; box-shadow:0 10px 25px rgba(0,0,0,0.07); padding:18px; margin-bottom:14px; }}
    .top {{ display:flex; gap:12px; flex-wrap:wrap; align-items:end; }}
    label {{ font-size:12px; color:#444; display:block; margin-bottom:6px; }}
    input {{ padding:10px 12px; border-radius:10px; border:1px solid #ddd; min-width:170px; background:#fff; }}
    button, a.btn {{ padding:10px 14px; border-radius:10px; border:0; cursor:pointer; background:#ff6f00; color:#fff; font-weight:700; text-decoration:none; display:inline-block; }}
    a.btn.secondary {{ background:#2f3a4a; }}
    .grid {{ display:grid; grid-template-columns:repeat(4,1fr); gap:10px; margin-top:12px; }}
    .kpi {{ background:#fafbff; border:1px solid #eceef6; border-radius:12px; padding:12px; }}
    .kpi .t {{ font-size:12px; color:#555; margin-bottom:6px; }}
    .kpi .v {{ font-size:18px; font-weight:800; }}
    table {{ width:100%; border-collapse:collapse; margin-top:12px; background:#fff; border-radius:12px; overflow:hidden; }}
    th, td {{ border-bottom:1px solid #eee; padding:10px; text-align:left; font-size:13px; vertical-align:top; }}
    th {{ background:#fff5ec; position:sticky; top:0; z-index:1; }}
    .muted {{ color:#666; font-size:12px; }}
    @media(max-width:900px){{ .grid{{grid-template-columns:repeat(2,1fr);}} }}
    @media(max-width:560px){{ .grid{{grid-template-columns:1fr;}} input{{min-width:140px;}} }}
  </style>
</head>
<body>
  <div class="wrap">
    <div class="card">
      <h2 style="margin:0 0 10px 0;">📊 Trendyol Kar/Zarar</h2>
      <div class="top">
        <div>
          <label>Başlangıç</label>
          <input id="start" type="date" value="{week_ago.isoformat()}">
        </div>
        <div>
          <label>Bitiş</label>
          <input id="end" type="date" value="{today.isoformat()}">
        </div>
        <div>
          <button onclick="loadAll()">Getir</button>
          <a id="excelLink" class="btn secondary" href="#" onclick="setExcelHref(); return true;">Excel İndir</a>
          <a id="debugLink" class="btn" style="background:#6b7280" href="#" onclick="setDebugHref(); return true;">Debug Sample</a>
        </div>
      </div>

      <div class="muted" style="margin-top:10px;">
        Hesap: Satış - (Komisyon + Kargo + Satıcı İndirim + Fatura %{int(INVOICE_RATE*100)}). Trendyol indirimini ayrıca gösteriyoruz.
      </div>
    </div>

    <div class="card">
      <div class="grid">
        <div class="kpi"><div class="t">Sipariş</div><div class="v" id="kpi_siparis">-</div></div>
        <div class="kpi"><div class="t">Satış Toplam</div><div class="v" id="kpi_satis">-</div></div>
        <div class="kpi"><div class="t">Toplam Kesinti</div><div class="v" id="kpi_kesinti">-</div></div>
        <div class="kpi"><div class="t">Net Kâr</div><div class="v" id="kpi_net">-</div></div>
      </div>
    </div>

    <div class="card">
      <h3 style="margin:0;">📦 Ürün Bazlı Detay</h3>
      <div class="muted">Satır sayısı: <span id="lineCount">-</span></div>
      <div style="max-height: 520px; overflow:auto; margin-top:10px;">
        <table>
          <thead>
            <tr>
              <th>Sipariş</th>
              <th>Ürün</th>
              <th>Kampanya</th>
              <th>Satış</th>
              <th>Komisyon</th>
              <th>Kargo</th>
              <th>Satıcı İndirim</th>
              <th>Trendyol İndirim</th>
              <th>Fatura %{int(INVOICE_RATE*100)}</th>
              <th>Net Kâr</th>
            </tr>
          </thead>
          <tbody id="tbody">
            <tr><td colspan="10" class="muted">Tarih seçip "Getir"e bas.</td></tr>
          </tbody>
        </table>
      </div>
    </div>
  </div>

<script>
function money(x) {{
  try {{
    const n = Number(x || 0);
    return n.toLocaleString('tr-TR', {{minimumFractionDigits:2, maximumFractionDigits:2}});
  }} catch(e) {{
    return x;
  }}
}}

function qs() {{
  const s = document.getElementById('start').value;
  const e = document.getElementById('end').value;
  return {{s, e}};
}}

function setExcelHref() {{
  const {{s, e}} = qs();
  document.getElementById('excelLink').href = `/report/excel?start=${{encodeURIComponent(s)}}&end=${{encodeURIComponent(e)}}`;
}}

function setDebugHref() {{
  const {{s, e}} = qs();
  document.getElementById('debugLink').href = `/debug/line-sample?start=${{encodeURIComponent(s)}}&end=${{encodeURIComponent(e)}}`;
}}

async function loadSummary() {{
  const {{s, e}} = qs();
  const res = await fetch(`/report?start=${{encodeURIComponent(s)}}&end=${{encodeURIComponent(e)}}`);
  const data = await res.json();

  document.getElementById('kpi_siparis').innerText = data.siparis ?? '-';
  document.getElementById('kpi_satis').innerText = money(data.satis_toplam);

  const kesinti =
    (data.komisyon_toplam || 0) +
    (data.kargo_toplam || 0) +
    (data.satici_indirim_toplam || 0) +
    (data['fatura_%{int(INVOICE_RATE*100)}_toplam'] || 0);

  document.getElementById('kpi_kesinti').innerText = money(kesinti);
  document.getElementById('kpi_net').innerText = money(data.net_kar_toplam);
}}

async function loadLines() {{
  const {{s, e}} = qs();
  const res = await fetch(`/report/lines?start=${{encodeURIComponent(s)}}&end=${{encodeURIComponent(e)}}`);
  const data = await res.json();

  document.getElementById('lineCount').innerText = data.adet ?? 0;

  const tb = document.getElementById('tbody');
  tb.innerHTML = '';

  const rows = (data.rows || []);
  if (!rows.length) {{
    tb.innerHTML = `<tr><td colspan="10" class="muted">Bu aralıkta satır bulunamadı.</td></tr>`;
    return;
  }}

  for (const r of rows) {{
    const tr = document.createElement('tr');
    tr.innerHTML = `
      <td>${{r['Sipariş'] || ''}}</td>
      <td>${{r['Ürün'] || ''}}</td>
      <td>${{r['Kampanya'] || ''}}</td>
      <td>${{money(r['Satış'])}}</td>
      <td>${{money(r['Komisyon'])}}</td>
      <td>${{money(r['Kargo'])}}</td>
      <td>${{money(r['Satıcı İndirim'])}}</td>
      <td>${{money(r['Trendyol İndirim'])}}</td>
      <td>${{money(r['Fatura %{int(INVOICE_RATE*100)}'])}}</td>
      <td><b>${{money(r['Net Kâr'])}}</b></td>
    `;
    tb.appendChild(tr);
  }}
}}

async function loadAll() {{
  setExcelHref();
  setDebugHref();
  const tb = document.getElementById('tbody');
  tb.innerHTML = `<tr><td colspan="10" class="muted">Yükleniyor...</td></tr>`;
  try {{
    await loadSummary();
    await loadLines();
  }} catch(e) {{
    tb.innerHTML = `<tr><td colspan="10" class="muted">Hata: ${{e}}</td></tr>`;
  }}
}}

setExcelHref();
setDebugHref();
</script>
</body>
</html>
"""

# =================================================
# Bugün hızlı erişim
# =================================================
@app.get("/report/excel/today")
def report_excel_today():
    today = datetime.now().strftime("%Y-%m-%d")
    return report_excel(today, today)
