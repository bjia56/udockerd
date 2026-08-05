import argparse
import logging
import os
import sys
import typing

if typing.TYPE_CHECKING:
    import io

from udockerd import __version__, udocker_ctx
from udockerd.config import (
    check_curl_available,
    check_linux,
    check_tar_available,
    configure_udocker,
)
from udockerd.server import serve

logger = logging.getLogger("udockerd")


def build_parser() -> argparse.ArgumentParser:
    parser = argparse.ArgumentParser(
        prog="udockerd",
        description=(
            "Docker Engine API-compatible daemon backed by udocker.\n"
            "Runs proot/fakechroot containers with no root, no namespaces, no cgroups."
        ),
        epilog=(
            "examples:\n"
            "  udockerd\n"
            "      listen on 127.0.0.1:2375 (default)\n"
            "  udockerd --host 0.0.0.0 --port 2375\n"
            "      listen on all interfaces\n"
            "  udockerd -v --data-dir ~/.udockerd-data\n"
            "      verbose logging, custom udocker data directory\n"
            "\n"
            "then point the docker CLI/SDK at it:\n"
            "  export DOCKER_HOST=tcp://127.0.0.1:2375\n"
            "  docker ps\n"
        ),
        formatter_class=argparse.RawDescriptionHelpFormatter,
    )
    parser.add_argument(
        "--host",
        default=os.environ.get("UDOCKERD_HOST", "127.0.0.1"),
        help="address to bind to (env: UDOCKERD_HOST, default: %(default)s)",
    )
    parser.add_argument(
        "--port",
        type=int,
        default=int(os.environ.get("UDOCKERD_PORT", "2375")),
        help="port to listen on (env: UDOCKERD_PORT, default: %(default)s)",
    )
    parser.add_argument(
        "--data-dir",
        default=os.environ.get("UDOCKER_DIR"),
        metavar="DIR",
        help="udocker data directory for images/containers/layers "
        "(env: UDOCKER_DIR, default: ~/.udocker)",
    )
    parser.add_argument(
        "--dns",
        action="append",
        metavar="IP",
        help="nameserver for containers' /etc/resolv.conf (repeatable; "
        "env: UDOCKERD_DNS, comma-separated; default: 8.8.8.8, 8.8.4.4)",
    )
    parser.add_argument(
        "-v",
        "--verbose",
        action="count",
        default=0,
        help="increase log verbosity (-v: info, -vv: debug, -vvv: debug + proot trace)",
    )
    parser.add_argument(
        "-q",
        "--quiet",
        action="store_true",
        help="suppress all logging except warnings and errors",
    )
    parser.add_argument(
        "--version",
        action="version",
        version=f"%(prog)s {__version__}",
    )
    return parser


def configure_logging(verbose: int, quiet: bool) -> None:
    if quiet:
        level = logging.WARNING
    elif verbose >= 2:  # noqa: PLR2004
        level = logging.DEBUG
    elif verbose == 1:
        level = logging.INFO
    else:
        level = logging.WARNING
    logging.basicConfig(level=level, format="%(asctime)s %(levelname)s %(name)s: %(message)s")


def main() -> int:
    # udocker's Msg class writes to stdout/stderr with no flush(); Python
    # full-buffers those when not a tty (backgrounded), delaying output.
    # Force line buffering so it behaves like a tty either way.
    typing.cast("io.TextIOWrapper", sys.stdout).reconfigure(line_buffering=True)
    typing.cast("io.TextIOWrapper", sys.stderr).reconfigure(line_buffering=True)

    args = build_parser().parse_args()
    configure_logging(args.verbose, args.quiet)

    check_linux()
    check_curl_available()
    check_tar_available()
    configure_udocker(args.data_dir)
    dns_servers = args.dns or [
        s for s in os.environ.get("UDOCKERD_DNS", "").split(",") if s
    ]
    udocker_ctx.set_dns_servers(dns_servers)
    udocker_ctx.init(verbose=args.verbose, quiet=args.quiet)

    logger.info("udockerd %s starting on %s:%d", __version__, args.host, args.port)
    serve(args.host, args.port)
    return 0


if __name__ == "__main__":
    sys.exit(main())
