from fastapi import FastAPI

app = FastAPI()

@app.get("/")
def root():
    return {"ok": True}
from fastapi import FastAPI

app = FastAPI()

@app.get("/")
def root():
    return {"ok": True}


@app.get("/health")
def health():
    return {
        "service": "trendyol-backend",
        "status": "running"
    }import os

@app.get("/env")
def env_check():
    return {
        "API_KEY_SET": bool(os.getenv("TRENDYOL_API_KEY")),
        "API_SECRET_SET": bool(os.getenv("TRENDYOL_API_SECRET")),
        "SELLER_ID_SET": bool(os.getenv("TRENDYOL_SELLER_ID"))
    }

