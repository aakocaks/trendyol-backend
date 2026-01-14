from fastapi import FastAPI
import os

from fastapi import Depends, HTTPException, status
from fastapi.security import HTTPBasic, HTTPBasicCredentials
import secrets

security = HTTPBasic()

def check_auth(credentials: HTTPBasicCredentials = Depends(security)):
    correct_user = secrets.compare_digest(
        credentials.username,
        os.getenv("PANEL_USER", "")
    )
    correct_pass = secrets.compare_digest(
        credentials.password,
        os.getenv("PANEL_PASS", "")
    )

    if not (correct_user and correct_pass):
        raise HTTPException(
            status_code=status.HTTP_401_UNAUTHORIZED,
            detail="Yetkisiz",
            headers={"WWW-Authenticate": "Basic"},
        )

app = FastAPI()

@app.get("/")
def root():
    return {"ok": True}

@app.get("/health")
def health():
    return {
        "service": "trendyol-backend",
        "status": "running"
    }

@app.get("/env")
def env_check():
    return {
        "API_KEY_SET": bool(os.getenv("TRENDYOL_API_KEY")),
        "API_SECRET_SET": bool(os.getenv("TRENDYOL_API_SECRET")),
        "SELLER_ID_SET": bool(os.getenv("TRENDYOL_SELLER_ID")),
        "MAIL_USER_SET": bool(os.getenv("MAIL_USER")),
        "MAIL_PASS_SET": bool(os.getenv("MAIL_PASS")),
        "MAIL_TO_SET": bool(os.getenv("MAIL_TO")),
    }
import requests
import base64

@app.get("/orders")
def get_orders():
    api_key = os.getenv("TRENDYOL_API_KEY")
    api_secret = os.getenv("TRENDYOL_API_SECRET")
    seller_id = os.getenv("TRENDYOL_SELLER_ID")

    # Güvenlik kontrolü
    if not api_key or not api_secret or not seller_id:
        return {"error": "Trendyol env bilgileri eksik"}

    auth = f"{api_key}:{api_secret}"
    encoded_auth = base64.b64encode(auth.encode()).decode()

    url = f"https://api.trendyol.com/sapigw/suppliers/{seller_id}/orders"

    headers = {
        "Authorization": f"Basic {encoded_auth}",
        "User-Agent": f"{seller_id} - Trendyol API"
    }

    response = requests.get(url, headers=headers)
    response.raise_for_status()

    return response.json()
from datetime import datetime

@app.get("/summary")
def summary(start: str, end: str):
    orders_response = get_orders()

    orders = orders_response.get("content", [])

    start_ts = int(datetime.strptime(start, "%Y-%m-%d").timestamp() * 1000)
    end_ts = int(datetime.strptime(end, "%Y-%m-%d").timestamp() * 1000)

    toplam_siparis = 0
    toplam_ciro = 0.0
    toplam_komisyon = 0.0
    toplam_kargo = 0.0

    for order in orders:
        order_date = order.get("orderDate")
        if not order_date:
            continue

        if not (start_ts <= order_date <= end_ts):
            continue

        toplam_siparis += 1

        for line in order.get("lines", []):
            toplam_ciro += float(line.get("price") or 0)
            toplam_komisyon += float(line.get("commission") or 0)
            toplam_kargo += float(line.get("cargoPrice") or 0)

    fatura_kdv = toplam_ciro * 0.10
    net_kar = toplam_ciro - toplam_komisyon - toplam_kargo - fatura_kdv

    return {
        "baslangic": start,
        "bitis": end,
        "toplam_siparis": toplam_siparis,
        "toplam_ciro": round(toplam_ciro, 2),
        "toplam_komisyon": round(toplam_komisyon, 2),
        "toplam_kargo": round(toplam_kargo, 2),
        "fatura_%10": round(fatura_kdv, 2),
        "net_kar": round(net_kar, 2)
    }
from fastapi.responses import FileResponse
from openpyxl import Workbook
import tempfile

@app.get("/summary/excel")
def summary_excel(start: str, end: str):
    orders_response = get_orders()
    orders = orders_response.get("content", [])

    start_ts = int(datetime.strptime(start, "%Y-%m-%d").timestamp() * 1000)
    end_ts = int(datetime.strptime(end, "%Y-%m-%d").timestamp() * 1000)

    toplam_siparis = 0
    toplam_ciro = 0.0
    toplam_komisyon = 0.0
    toplam_kargo = 0.0

    for order in orders:
        order_date = order.get("orderDate")
        if not order_date:
            continue

        if not (start_ts <= order_date <= end_ts):
            continue

        toplam_siparis += 1

        for line in order.get("lines", []):
            toplam_ciro += float(line.get("price") or 0)
            toplam_komisyon += float(line.get("commission") or 0)
            toplam_kargo += float(line.get("cargoPrice") or 0)

    fatura_kdv = toplam_ciro * 0.10
    net_kar = toplam_ciro - toplam_komisyon - toplam_kargo - fatura_kdv

    # Excel oluştur
    wb = Workbook()
    ws = wb.active
    ws.title = "Kar_Zarar"

    ws.append(["Alan", "Tutar"])
    ws.append(["Toplam Sipariş", toplam_siparis])
    ws.append(["Toplam Ciro", round(toplam_ciro, 2)])
    ws.append(["Toplam Komisyon", round(toplam_komisyon, 2)])
    ws.append(["Toplam Kargo", round(toplam_kargo, 2)])
    ws.append(["Fatura %10", round(fatura_kdv, 2)])
    ws.append(["Net Kar", round(net_kar, 2)])

    tmp = tempfile.NamedTemporaryFile(delete=False, suffix=".xlsx")
    wb.save(tmp.name)

    return FileResponse(
        tmp.name,
        media_type="application/vnd.openxmlformats-officedocument.spreadsheetml.sheet",
        filename=f"kar_zarar_{start}_{end}.xlsx"
    )
from datetime import datetime

@app.get("/summary/excel/today")
def summary_excel_today():
    today = datetime.now().strftime("%Y-%m-%d")
    return summary_excel(start=today, end=today)
import pandas as pd
from fastapi.responses import StreamingResponse
from io import BytesIO

@app.get("/orders/excel")
def orders_excel():
    import os, base64, requests

    api_key = os.getenv("TRENDYOL_API_KEY")
    api_secret = os.getenv("TRENDYOL_API_SECRET")
    seller_id = os.getenv("TRENDYOL_SELLER_ID")

    auth = f"{api_key}:{api_secret}"
    encoded_auth = base64.b64encode(auth.encode()).decode()

    url = f"https://api.trendyol.com/sapigw/suppliers/{seller_id}/orders"

    headers = {
        "Authorization": f"Basic {encoded_auth}",
        "User-Agent": f"{seller_id} - Trendyol API"
    }

    r = requests.get(url, headers=headers)
    r.raise_for_status()
    data = r.json()

    rows = []

    for order in data.get("content", []):
        order_no = order.get("orderNumber")
        for line in order.get("lines", []):
            price = line.get("price", 0)
            commission = line.get("commission", 0)
            cargo = line.get("cargoPrice", 0)
            kdv = price * 0.10
            net = price - commission - cargo - kdv

            rows.append({
                "Sipariş No": order_no,
                "Ürün": line.get("productName"),
                "Adet": line.get("quantity"),
                "Satış Fiyatı": price,
                "Komisyon": commission,
                "Kargo": cargo,
                "%10 Fatura": round(kdv, 2),
                "Net Kâr": round(net, 2)
            })

    df = pd.DataFrame(rows)

    output = BytesIO()
    df.to_excel(output, index=False)
    output.seek(0)

    return StreamingResponse(
        output,
        media_type="application/vnd.openxmlformats-officedocument.spreadsheetml.sheet",
        headers={"Content-Disposition": "attachment; filename=siparis_detay.xlsx"}
    )
@app.get("/summary/excel/month")
def summary_excel_month(year: int, month: int):
    from datetime import datetime
    import calendar

    start_date = f"{year}-{month:02d}-01"
    last_day = calendar.monthrange(year, month)[1]
    end_date = f"{year}-{month:02d}-{last_day}"

    return summary_excel(start=start_date, end=end_date)
from fastapi.responses import HTMLResponse

@app.get("/panel")
def panel(key: str):
    check_panel_key(key)
    return """
    <html>
    <head>
        <title>Trendyol Panel</title>
        <style>
            body { font-family: Arial; background:#f5f6fa; padding:40px; }
            h1 { margin-bottom:20px; }
            input, button {
                padding:10px;
                margin:8px;
                font-size:15px;
            }
        </style>
    </head>
    <body>
        <h1>📊 Trendyol Rapor Paneli</h1>

        <h3>📅 Tarih Aralığı Özet Excel</h3>
        <input type="date" id="start">
        <input type="date" id="end">
        <button onclick="downloadSummary()">📥 Özet Excel</button>

        <h3>📦 Sipariş Detay</h3>
        <button onclick="window.location.href='/orders/excel?key=""" + key + """'">
            📥 Sipariş Detay Excel
        </button>

        <script>
            function downloadSummary() {
                const start = document.getElementById('start').value;
                const end = document.getElementById('end').value;

                if (!start || !end) {
                    alert("Tarih seç kanka 🙂");
                    return;
                }

                window.location.href =
                    `/summary/excel?start=${start}&end=${end}&key=` + """ + key + """;
            }
        </script>
    </body>
    </html>
    """

