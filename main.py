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
    }
