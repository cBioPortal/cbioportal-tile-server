import os

bind = "0.0.0.0:8080"
worker_class = "uvicorn.workers.UvicornWorker"
workers = int(os.environ.get("N_WORKERS", "4"))
preload_app = True
timeout = int(os.environ.get("GUNICORN_TIMEOUT", "180"))
loglevel = os.environ.get("GUNICORN_LOG_LEVEL", "info")
# The container runs without a writable home directory.  Gunicorn's optional
# control socket otherwise defaults below /home/appuser and emits a startup
# error even though the HTTP workers are healthy.
control_socket_disable = True


def child_exit(server, worker):
    """Remove dead worker metrics from Prometheus multiprocess gauges."""
    if os.environ.get("PROMETHEUS_MULTIPROC_DIR"):
        from prometheus_client import multiprocess

        multiprocess.mark_process_dead(worker.pid)
