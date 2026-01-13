from fastapi import FastAPI
from fastapi.responses import FileResponse
import os
import requests
import base64
from datetime import datetime
from openpyxl import Workbook

app = FastAPI()

@app.get("/")
def root():
    return {"status": "ok", "env_loaded": True}

@app.get("/health")
def health():
    return {"service": "trendyol-backend", "status": "running"}

def get_orders():
    api_key = os.getenv("TRENDYOL_API_KEY")
    api_secret = os.getenv("TRENDYOL_API_SECRET")
    seller_id = os.getenv("TRENDYOL_SELLER_ID")

    auth = f"{api_key}:{api_secret}"
    encoded = base64.b64encode(auth.encode()).decode()

    url = f"https://api.trendyol.com/sapigw/suppliers/{seller_id}/orders"
    headers = {
        "Authorization": f"Basic {encoded}",
        "User-Agent": f"{seller_id} - Trendyol API"
    }

    r = requests.get(url, headers=headers)
    r.raise_for_status()
    return r.json().get("content", [])

@app.get("/report/today")
def today_report():
    orders = get_orders()

    toplam_ciro = 0
    toplam_komisyon = 0
    toplam_kargo = 0
    toplam_siparis = 0

    today = datetime.now().date()

    for order in orders:
        order_date = datetime.fromtimestamp(order["orderDate"] / 1000).date()
        if order_date == today:
            toplam_siparis += 1
            for line in order.get("lines", []):
                toplam_ciro += line.get("price", 0)
                toplam_komisyon += line.get("commission", 0)
                toplam_kargo += line.get("cargoPrice", 0)

    kdv = toplam_ciro * 0.10
    net_kar = toplam_ciro - toplam_komisyon - toplam_kargo - kdv

    wb = Workbook()
    ws = wb.active
    ws.title = "Özet"

    ws.append(["Alan", "Tutar"])
    ws.append(["Toplam Sipariş", toplam_siparis])
    ws.append(["Toplam Ciro", round(toplam_ciro, 2)])
    ws.append(["Toplam Komisyon", round(toplam_komisyon, 2)])
    ws.append(["Toplam Kargo", round(toplam_kargo, 2)])
    ws.append(["KDV (%10)", round(kdv, 2)])
    ws.append(["Net Kar", round(net_kar, 2)])

    filename = f"trendyol_rapor_{today}.xlsx"
    filepath = f"/tmp/{filename}"
    wb.save(filepath)

    return FileResponse(
        filepath,
        media_type="application/vnd.openxmlformats-officedocument.spreadsheetml.sheet",
        filename=filename
    )
