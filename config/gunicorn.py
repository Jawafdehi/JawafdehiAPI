"""Gunicorn config — django-prometheus multiprocess support.

With >1 gunicorn worker, django-prometheus must run in prometheus_client
*multiprocess* mode: each worker writes per-pid metric files under
``PROMETHEUS_MULTIPROC_DIR`` and the ``/metrics`` view aggregates across them.
This config provides the two lifecycle hooks that mode needs so the aggregate
stays correct as gunicorn forks and recycles workers:

* ``on_starting``  — clear stale ``*.db`` files from a previous run (belt-and-
  suspenders; the deploy also mounts a fresh emptyDir per pod).
* ``child_exit``   — mark a dead worker's pid so its gauge files stop being
  counted, otherwise recycled workers double-count.

Only meaningful when ``PROMETHEUS_MULTIPROC_DIR`` is set (prod). If it is unset
(local/dev, tests) both hooks are no-ops.
"""

import glob
import os


def on_starting(server):
    d = os.environ.get("PROMETHEUS_MULTIPROC_DIR")
    if not d or not os.path.isdir(d):
        return
    for f in glob.glob(os.path.join(d, "*.db")):
        try:
            os.remove(f)
        except OSError:
            pass


def child_exit(server, worker):
    if not os.environ.get("PROMETHEUS_MULTIPROC_DIR"):
        return
    from prometheus_client import multiprocess

    multiprocess.mark_process_dead(worker.pid)
