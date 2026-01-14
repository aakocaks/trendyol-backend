from fastapi import FastAPI, Query
from fastapi.responses import HTMLResponse, FileResponse
from datetime import datetime
import pandas as pd
import os
import uuid

app = FastAPI()

# ---------------------------
# SAHTE ORDER VERİSİ (SENDE API VARSA BURAYI DEĞİŞTİRME)
# ---------------------------
def get_orders():
    return [
        {
            "orderDate": 1705000000000,
            "totalPrice": 500,
            "commission": 50,
            "cargoPrice": 40
        },
        {
            "orderDate": 1705100000000,
            "totalPrice": 300,
            "commission": 30,
            "cargoPrice": 25
        }
    ]

# ---------------------------
# HEALTH
# ---------------------------
@app.get("/")
def root():
    return {"status": "ok"}

# ---------------------------
# PANEL
# ---------------------------
@app.get("/panel", response_class=HTMLResponse)
def panel():
    return """
<!DOCTYPE html>
<html lang="tr">
<head>
<meta charset="UTF-8">
<title>Trendyol Panel</title>
<style>
body {
    margin: 0;
    height: 100vh;
    display: flex;
    justify-content: center;
    align-items: center;
    background: linear-gradient(135deg, #ff6f00, #ff9800);
    font-family: Arial, sans-serif;
}
.panel {
    background: white;
    padding: 30px;
    width: 360px;
    border-radius: 16px;
    box-shadow: 0 15px 35px rgba(0,0,0,.25);
    text-align: center;
}
h1 {
    color: #ff6f00;
    margin-bottom: 20px;
}
input {
    width: 100%;
    padding: 10px;
    margin-bottom: 10px;
}
button {
    width: 100%;
    padding: 12px;
    background: #ff6f00;
    color: white;
    border: none;
    border-radius: 8px;
    font-size: 16px;
    cursor: pointer;
}
.summary {
    margin-bottom: 15px;
    display: none;
    text-align: left;
}
</style>
</head>
<body>

<div class="panel">
    <h1>📊 Trendyol Panel</h1>

    <input type="date" id="start" onchange="loadSummary()">
    <input type="date" id="end" onchange="loadSummary()">

    <div class="summary" id="summary">
        <p>📦 Sipariş: <b id="s_siparis"></b></p>
        <p>💰 Ciro: <b id="s_ciro"></b> ₺</p>
        <p>💸 Komisyon: <b id="s_komisyon"></b> ₺</p>
        <p>🚚 Kargo: <b id="s_kargo"></b> ₺</p>
        <p>✅ Net Kar: <b id="s_kar"></b> ₺</p>
    </div>

    <button onclick="downloadExcel()">Excel İndir</button>
</div>

<script>
async function loadSummary() {
    const start = document.getElementById("start").value;
    const end = document.getElementById("end").value;
    if (!start || !end) return;

    const res = await fetch(`/summary?start=${start}&end=${end}`);
    const data = await res.json();

    document.getElementById("s_siparis").innerText = data.siparis;
    document.getElementById("s_ciro").innerText = data.ciro;
    document.getElementById("s_komisyon").innerText = data.komisyon;
    document.getElementById("s_kargo").innerText = data.kargo;
    document.getElementById("s_kar").innerText = data.net_kar;

    document.getElementById("summary").style.display = "block";
}

function downloadExcel() {
    const start = document.getElementById("start").value;
    const end = document.getElementById("end").value;
    if (!start || !end) {
        alert("Tarih seç");
        return;
    }
    window.location = `/excel?start=${start}&end=${end}`;
}
</script>

</body>
</html>
"""

# ---------------------------
# ÖZET API
# ---------------------------
@app.get("/summary")
def summary(start: str = Query(...), end: str = Query(...)):
    orders = get_orders()
    start_date = datetime.strptime(start, "%Y-%m-%d").date()
    end_date = datetime.strptime(end, "%Y-%m-%d").date()

    siparis = ciro = komisyon = kargo = 0

    for o in orders:
        d = datetime.fromtimestamp(o["orderDate"] / 1000).date()
        if start_date <= d <= end_date:
            siparis += 1
            ciro += o["totalPrice"]
            komisyon += o["commission"]
            kargo += o["cargoPrice"]

    return {
        "siparis": siparis,
        "ciro": round(ciro, 2),
        "komisyon": round(komisyon, 2),
        "kargo": round(kargo, 2),
        "net_kar": round(ciro - komisyon - kargo, 2)
    }

# ---------------------------
# EXCEL
# ---------------------------
@app.get("/excel")
def excel(start: str, end: str):
    orders = get_orders()
    rows = []

    start_date = datetime.strptime(start, "%Y-%m-%d").date()
    end_date = datetime.strptime(end, "%Y-%m-%d").date()

    for o in orders:
        d = datetime.fromtimestamp(o["orderDate"] / 1000).date()
        if start_date <= d <= end_date:
            rows.append({
                "Tarih": d,
                "Ciro": o["totalPrice"],
                "Komisyon": o["commission"],
                "Kargo": o["cargoPrice"],
                "Net": o["totalPrice"] - o["commission"] - o["cargoPrice"]
            })

    df = pd.DataFrame(rows)
    file_name = f"rapor_{uuid.uuid4().hex}.xlsx"
    df.to_excel(file_name, index=False)

    return FileResponse(
        file_name,
        filename="trendyol_rapor.xlsx",
        media_type="application/vnd.openxmlformats-officedocument.spreadsheetml.sheet"
    )
