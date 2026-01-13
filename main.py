from fastapi import FastAPI
from fastapi.responses import FileResponse
import os
import requests
import base64
from datetime import datetime
from openpyxl import Workbook

app = FastAPI()

# -------------------
# GENEL KONTROLLER
# -------------------

@app.get("/")
def root():
    return {"status": "ok"}

@app.get("/health")
def health():
    return {"service": "trendyol-backend", "status": "running"}

@app.get("/env")
def env_check():
    return {
        "TRENDYOL_API_KEY": bool(os.getenv("TRENDYOL_API_KEY")),
        "TRENDYOL_API_SECRET": bool(os.getenv("TRENDYOL_API_SECRET")),
        "TRENDYOL_SELLER_ID": bool(os.getenv("TRENDYOL_SELLER_ID")),
    }

# -------------------
# TRENDYOL ORDERS
# -------------------

def get_orders():
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

    return r.json().get("content", [])

# -------------------
# BUGÜNLÜK EXCEL RAPOR
# -------------------

@app.get("/report/today")
def report_today():
    orders = get_orders()
    today = datetime.now().date()

    toplam_siparis = 0
    toplam_ciro = 0.0
    toplam_komisyon = 0.0
    toplam_kargo = 0.0

    for order in orders:
        order_date_ms = order.get("orderDate")
        if not order_date_ms:
            continue

        order_date = datetime.fromtimestamp(order_date_ms / 1000).date()
        if order_date != today:
            continue

        toplam_siparis += 1

        for line in order.get("lines", []):
            toplam_ciro += float(line.get("price") or 0)
            toplam_komisyon += float(line.get("commission") or 0)
            toplam_kargo += float(line.get("cargoPrice") or 0)

    kdv = toplam_ciro * 0.10
    net_kar = toplam_ciro - toplam_komisyon - toplam_kargo - kdv

    # Excel oluştur
    wb = Workbook()
    ws = wb.active
    ws.title = "Gunluk Ozet"

    ws.append(["Alan", "Tutar"])
    ws.append(["Toplam Sipariş", toplam_siparis])
    ws.append(["Toplam Ciro", round(toplam_ciro, 2)])
    ws.append(["Toplam Komisyon", round(toplam_komisyon, 2)])
    ws.append(["Toplam Kargo", round(toplam_kargo, 2)])
    ws.append(["KDV (%10)", round(kdv, 2)])
    ws.append(["Net Kar", round(net_kar, 2)])

    filename = f"trendyol_gunluk_rapor_{today}.xlsx"
    filepath = f"/tmp/{filename}"
    wb.save(filepath)

    return FileResponse(
        filepath,
        media_type="application/vnd.openxmlformats-officedocument.spreadsheetml.sheet",
        filename=filename
    )
