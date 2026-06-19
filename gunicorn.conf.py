"""Gunicorn config.

Carries the same bind/worker settings the Dockerfile CMD used to pass as flags,
plus the multiprocess hook django-prometheus needs: with >1 worker process each
worker writes its metrics to a shared dir (PROMETHEUS_MULTIPROC_DIR, an emptyDir
in the k8s deployment) and the /metrics view aggregates across them. When a worker
dies its db files must be reaped so its counters stop being double-counted.
"""

bind = "0.0.0.0:8080"
workers = 2
threads = 4
timeout = 60
accesslog = "-"
errorlog = "-"


def child_exit(server, worker):
    from prometheus_client import multiprocess

    multiprocess.mark_process_dead(worker.pid)
