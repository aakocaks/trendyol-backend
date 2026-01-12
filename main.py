from fastapi import FastAPI
import os

app = FastAPI()

@app.get("/")
def root():
    return {
        "status": "ok",
        "env_loaded": bool(os.getenv("TEST_ENV"))
    }
