from fastapi import FastAPI
from fastapi.responses import FileResponse, StreamingResponse, HTMLResponse
import os, base64, requests, tempfile
from datetime import datetime
from openpyxl import Workbook
import pandas as pd
from io import BytesIO

app = FastAPI()

# -------------------------------------------------
# KONTROLLER
# -------------------------------------------------

@app.get("/")
def root():
    return {"ok": True}

@app.get("/health")
def health():
    return {"status": "running"}

@app.get("/env")
def env_check():
    return {
        "API_KEY_SET": bool(os.getenv("TRENDYOL_API_KEY")),
        "API_SECRET_SET": bool(os.getenv("TRENDYOL_API_SECRET")),
        "SELLER_ID_SET": bool(os.getenv("TRENDYOL_SELLER_ID")),
    }

# -------------------------------------------------
# TRENDYOL ORDERS
# -------------------------------------------------

def fetch_orders():
    api_key = os.getenv("TRENDYOL_API_KEY")
    api_secret = os.getenv("TRENDYOL_API_SECRET")
    seller_id = os.getenv("TRENDYOL_SELLER_ID")

    if not api_key or not api_secret or not seller_id:
        raise Exception("ENV eksik")

    auth = base64.b64encode(f"{api_key}:{api_secret}".encode()).decode()

    url = f"https://api.trendyol.com/sapigw/suppliers/{seller_id}/orders"
    headers = {
        "Authorization": f"Basic {auth}",
        "User-Agent": f"{seller_id} - Trendyol API"
    }

    r = requests.get(url, headers=headers)
    r.raise_for_status()
    return r.json().get("content", [])

@app.get("/orders")
def orders():
    return fetch_orders()

# -------------------------------------------------
# SUMMARY JSON
# -------------------------------------------------

@app.get("/summary")
def summary(start: str, end: str):
    orders = fetch_orders()

    start_ts = int(datetime.strptime(start, "%Y-%m-%d").timestamp() * 1000)
    end_ts = int(datetime.strptime(end, "%Y-%m-%d").timestamp() * 1000)

    siparis = ciro = komisyon = kargo = 0.0

    for o in orders:
        if not (start_ts <= o["orderDate"] <= end_ts):
            continue
        siparis += 1
        for l in o["lines"]:
            ciro += l.get("price", 0)
            komisyon += l.get("commission", 0)
            kargo += l.get("cargoPrice", 0)

    fatura = ciro * 0.10
    net = ciro - komisyon - kargo - fatura

    return {
        "siparis": int(siparis),
        "ciro": round(ciro, 2),
        "komisyon": round(komisyon, 2),
        "kargo": round(kargo, 2),
        "fatura_%10": round(fatura, 2),
        "net_kar": round(net, 2)
    }

# -------------------------------------------------
# SUMMARY EXCEL
# -------------------------------------------------

@app.get("/summary/excel")
def summary_excel(start: str, end: str):
    data = summary(start, end)

    wb = Workbook()
    ws = wb.active
    ws.append(["Alan", "Tutar"])

    for k, v in data.items():
        ws.append([k, v])

    tmp = tempfile.NamedTemporaryFile(delete=False, suffix=".xlsx")
    wb.save(tmp.name)

    return FileResponse(
        tmp.name,
        filename=f"kar_zarar_{start}_{end}.xlsx",
        media_type="application/vnd.openxmlformats-officedocument.spreadsheetml.sheet"
    )

@app.get("/summary/excel/today")
def today_excel():
    today = datetime.now().strftime("%Y-%m-%d")
    return summary_excel(today, today)

@app.get("/summary/excel/month")
def month_excel(year: int, month: int):
    import calendar
    last_day = calendar.monthrange(year, month)[1]
    start = f"{year}-{month:02d}-01"
    end = f"{year}-{month:02d}-{last_day}"
    return summary_excel(start, end)

# -------------------------------------------------
# ORDERS DETAIL EXCEL
# -------------------------------------------------

@app.get("/orders/excel")
def orders_excel():
    orders = fetch_orders()
    rows = []

    for o in orders:
        for l in o["lines"]:
            price = l.get("price", 0)
            commission = l.get("commission", 0)
            cargo = l.get("cargoPrice", 0)
            kdv = price * 0.10
            net = price - commission - cargo - kdv

            rows.append({
                "Sipariş No": o.get("orderNumber"),
                "Ürün": l.get("productName"),
                "Adet": l.get("quantity"),
                "Fiyat": price,
                "Komisyon": commission,
                "Kargo": cargo,
                "%10 Fatura": round(kdv, 2),
                "Net Kar": round(net, 2)
            })

    df = pd.DataFrame(rows)
    output = BytesIO()
    df.to_excel(output, index=False)
    output.seek(0)

    return StreamingResponse(
        output,
        media_type="application/vnd.openxmlformats-officedocument.spreadsheetml.sheet",
        headers={"Content-Disposition": "attachment; filename=siparisler.xlsx"}
    )

# -------------------------------------------------
# PANEL
# -------------------------------------------------

@app.get("/panel", response_class=HTMLResponse)
def panel():
    return """
    <h1>📊 Trendyol Panel</h1>
    <ul>
        <li><a href="/summary/excel/today">Bugün Kar/Zarar</a></li>
        <li><a href="/orders/excel">Sipariş Detay Excel</a></li>
        <li><a href="/summary/excel/month?year=2026&month=1">Aylık Excel</a></li>
    </ul>
    """
