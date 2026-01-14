from fastapi import FastAPI, Depends, HTTPException, status
from fastapi.security import HTTPBasic, HTTPBasicCredentials
from fastapi.responses import FileResponse, StreamingResponse, HTMLResponse
import os, base64, requests, tempfile
from datetime import datetime
from openpyxl import Workbook
import pandas as pd
from io import BytesIO

app = FastAPI()
security = HTTPBasic()

# -------------------------------------------------
# PANEL AUTH
# -------------------------------------------------

def panel_auth(credentials: HTTPBasicCredentials = Depends(security)):
    user = os.getenv("PANEL_USER")
    password = os.getenv("PANEL_PASS")

    if credentials.username != user or credentials.password != password:
        raise HTTPException(
            status_code=status.HTTP_401_UNAUTHORIZED,
            detail="Yetkisiz",
            headers={"WWW-Authenticate": "Basic"},
        )

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
        "PANEL_AUTH_SET": bool(os.getenv("PANEL_USER") and os.getenv("PANEL_PASS"))
    }

# -------------------------------------------------
# TRENDYOL ORDERS
# -------------------------------------------------

def fetch_orders():
    api_key = os.getenv("TRENDYOL_API_KEY")
    api_secret = os.getenv("TRENDYOL_API_SECRET")
    seller_id = os.getenv("TRENDYOL_SELLER_ID")

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
# SUMMARY
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
# EXCEL
# -------------------------------------------------

@app.get("/summary/excel/today")
def today_excel():
    today = datetime.now().strftime("%Y-%m-%d")
    return summary_excel(today, today)

def summary_excel(start, end):
    data = summary(start, end)
    wb = Workbook()
    ws = wb.active
    ws.append(["Alan", "Tutar"])
    for k, v in data.items():
        ws.append([k, v])

    tmp = tempfile.NamedTemporaryFile(delete=False, suffix=".xlsx")
    wb.save(tmp.name)

    return FileResponse(tmp.name, filename="kar_zarar.xlsx")

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
                "Sipariş": o.get("orderNumber"),
                "Ürün": l.get("productName"),
                "Fiyat": price,
                "Komisyon": commission,
                "Kargo": cargo,
                "Fatura %10": kdv,
                "Net Kar": net
            })

    df = pd.DataFrame(rows)
    out = BytesIO()
    df.to_excel(out, index=False)
    out.seek(0)

    return StreamingResponse(out, headers={
        "Content-Disposition": "attachment; filename=siparisler.xlsx"
    })

# -------------------------------------------------
# 🔐 ŞİFRELİ PANEL
# -------------------------------------------------

@app.get("/panel", response_class=HTMLResponse)
def panel(auth=Depends(panel_auth)):
    return """
    <h1>🔐 Trendyol Panel</h1>
    <ul>
        <li><a href="/summary/excel/today">📊 Bugün Kar/Zarar</a></li>
        <li><a href="/orders/excel">📦 Sipariş Excel</a></li>
    </ul>
    """
