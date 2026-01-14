from fastapi import FastAPI, Depends, HTTPException, status, Query
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

# -------------------------------------------------
# SUMMARY
# -------------------------------------------------

def summary_data(start: str, end: str):
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
        "Sipariş": int(siparis),
        "Ciro": round(ciro, 2),
        "Komisyon": round(komisyon, 2),
        "Kargo": round(kargo, 2),
        "Fatura %10": round(fatura, 2),
        "Net Kar": round(net, 2)
    }

# -------------------------------------------------
# 📊 TARİH ARALIĞI EXCEL
# -------------------------------------------------

@app.get("/summary/excel")
def summary_excel(
    start: str = Query(..., example="2026-01-01"),
    end: str = Query(..., example="2026-01-13")
):
    data = summary_data(start, end)

    wb = Workbook()
    ws = wb.active
    ws.append(["Alan", "Tutar"])

    for k, v in data.items():
        ws.append([k, v])

    tmp = tempfile.NamedTemporaryFile(delete=False, suffix=".xlsx")
    wb.save(tmp.name)

    return FileResponse(tmp.name, filename="kar_zarar.xlsx")

# -------------------------------------------------
# 🔐 PANEL (TARİH SEÇMELİ)
# -------------------------------------------------

@app.get("/panel", response_class=HTMLResponse)
def panel(auth=Depends(panel_auth)):
    return """
<!DOCTYPE html>
<html lang="tr">
<head>
<meta charset="UTF-8">
<title>Trendyol Panel</title>
<style>
body {
    margin:0;
    height:100vh;
    display:flex;
    justify-content:center;
    align-items:center;
    background:linear-gradient(135deg,#ff6f00,#ff9800);
    font-family:Arial;
}
.panel {
    background:white;
    padding:30px;
    width:340px;
    border-radius:14px;
    box-shadow:0 10px 25px rgba(0,0,0,0.2);
}
h1 {
    text-align:center;
    color:#ff6f00;
}
label {
    font-weight:bold;
}
input, button {
    width:100%;
    padding:10px;
    margin-top:8px;
}
button {
    background:#ff6f00;
    color:white;
    border:none;
    border-radius:6px;
    font-size:16px;
    cursor:pointer;
}
button:hover {
    background:#e65c00;
}
</style>
</head>
<body>
<div class="panel">
<h1>📊 Trendyol Panel</h1>

<label>Başlangıç Tarihi</label>
<input type="date" id="start">

<label>Bitiş Tarihi</label>
<input type="date" id="end">

<button onclick="indir()">Excel İndir</button>

<script>
function indir() {
    const s = document.getElementById("start").value;
    const e = document.getElementById("end").value;
    if(!s || !e){
        alert("Tarih seç!");
        return;
    }
    window.location = `/summary/excel?start=${s}&end=${e}`;
}
</script>
</div>
</body>
</html>
"""
from fastapi import Query

@app.get("/summary")
def summary(
    start: str = Query(...),
    end: str = Query(...)
):
    orders = get_orders()

    start_date = datetime.strptime(start, "%Y-%m-%d").date()
    end_date = datetime.strptime(end, "%Y-%m-%d").date()

    toplam_siparis = 0
    toplam_ciro = 0.0
    toplam_komisyon = 0.0
    toplam_kargo = 0.0

    for order in orders:
        order_date_ms = order.get("orderDate")
        if not order_date_ms:
            continue

        order_date = datetime.fromtimestamp(order_date_ms / 1000).date()
        if not (start_date <= order_date <= end_date):
            continue

        toplam_siparis += 1
        toplam_ciro += order.get("totalPrice", 0)
        toplam_komisyon += order.get("commission", 0)
        toplam_kargo += order.get("cargoPrice", 0)

    net_kar = toplam_ciro - toplam_komisyon - toplam_kargo

    return {
        "siparis": toplam_siparis,
        "ciro": round(toplam_ciro, 2),
        "komisyon": round(toplam_komisyon, 2),
        "kargo": round(toplam_kargo, 2),
        "net_kar": round(net_kar, 2)
    }
