from udockerd.http import Router, ThreadingHTTPServer, make_handler_class
from udockerd.routes import images, system


def build_router() -> Router:
    router = Router()
    system.register(router)
    images.register(router)
    return router


def serve(host: str, port: int) -> None:
    router = build_router()
    handler_cls = make_handler_class(router)
    httpd = ThreadingHTTPServer((host, port), handler_cls)
    try:
        httpd.serve_forever()
    except KeyboardInterrupt:
        pass
    finally:
        httpd.shutdown()
