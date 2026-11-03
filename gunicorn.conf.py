bind = "0.0.0.0:8000"
workers = 1
threads = 4
timeout = 60
graceful_timeout = 30
accesslog = None
errorlog = "-"
worker_tmp_dir = "/tmp"
forwarded_allow_ips = ""  # app middleware trusts only configured peer IPs

def post_worker_init(worker):
    from los_worker import start
    start()

def worker_exit(server, worker):
    from los_worker import stop
    stop()
