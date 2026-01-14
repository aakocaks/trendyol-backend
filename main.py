from fastapi import FastAPI
import os

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
