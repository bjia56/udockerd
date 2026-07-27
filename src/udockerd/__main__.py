import argparse
import sys

from udockerd import udocker_ctx
from udockerd.config import check_curl_available, configure_udocker
from udockerd.server import serve


def main() -> int:
    parser = argparse.ArgumentParser(prog="udockerd")
    parser.add_argument("--host", default="127.0.0.1")
    parser.add_argument("--port", type=int, default=2375)
    args = parser.parse_args()

    check_curl_available()
    configure_udocker()
    udocker_ctx.init()

    serve(args.host, args.port)
    return 0


if __name__ == "__main__":
    sys.exit(main())
