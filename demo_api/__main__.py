import uvicorn

if __name__ == "__main__":
    uvicorn.run("demo_api.app:app", host="0.0.0.0", port=8000,
                proxy_headers=False, workers=1)
