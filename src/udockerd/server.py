import logging
import signal
import threading
from types import FrameType

from udockerd import container_proc
from udockerd.http import Router, ThreadingHTTPServer, make_handler_class
from udockerd.routes import build, containers, images, system
from udockerd.routes import exec as exec_routes

logger = logging.getLogger("udockerd")


def build_router() -> Router:
    router = Router()
    system.register(router)
    images.register(router)
    containers.register(router)
    exec_routes.register(router)
    build.register(router)
    return router


def serve(host: str, port: int) -> None:
    router = build_router()
    handler_cls = make_handler_class(router)
    httpd = ThreadingHTTPServer((host, port), handler_cls)

    def shutdown_gracefully() -> None:
        logger.info("shutting down, stopping containers")
        container_proc.stop_all()
        httpd.shutdown()
        logger.info("shutdown complete")

    def handle_sigterm(signum: int, frame: FrameType | None) -> None:
        # httpd.shutdown() blocks until serve_forever()'s loop (same
        # thread) notices, so it can't run inline from this handler.
        logger.info("received SIGTERM")
        threading.Thread(target=shutdown_gracefully, daemon=True).start()

    signal.signal(signal.SIGTERM, handle_sigterm)

    logger.info("listening on %s:%d", host, port)
    try:
        httpd.serve_forever()
    except KeyboardInterrupt:
        logger.info("received KeyboardInterrupt")
        shutdown_gracefully()
