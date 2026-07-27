import os

bind = "0.0.0.0:8080"
worker_class = "uvicorn.workers.UvicornWorker"
workers = int(os.environ.get("N_WORKERS", "4"))
preload_app = True
timeout = int(os.environ.get("GUNICORN_TIMEOUT", "120"))
loglevel = os.environ.get("GUNICORN_LOG_LEVEL", "info")
