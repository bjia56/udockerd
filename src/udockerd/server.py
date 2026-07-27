import signal
import threading
from types import FrameType

from udockerd import container_proc
from udockerd.http import Router, ThreadingHTTPServer, make_handler_class
from udockerd.routes import containers, images, system
from udockerd.routes import exec as exec_routes


def build_router() -> Router:
    router = Router()
    system.register(router)
    images.register(router)
    containers.register(router)
    exec_routes.register(router)
    return router


def serve(host: str, port: int) -> None:
    router = build_router()
    handler_cls = make_handler_class(router)
    httpd = ThreadingHTTPServer((host, port), handler_cls)

    def shutdown_gracefully() -> None:
        container_proc.stop_all()
        httpd.shutdown()

    def handle_sigterm(signum: int, frame: FrameType | None) -> None:
        # httpd.shutdown() blocks until serve_forever()'s loop (same
        # thread) notices, so it can't run inline from this handler.
        threading.Thread(target=shutdown_gracefully, daemon=True).start()

    signal.signal(signal.SIGTERM, handle_sigterm)

    try:
        httpd.serve_forever()
    except KeyboardInterrupt:
        shutdown_gracefully()
