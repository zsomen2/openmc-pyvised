"""Command line of the backend: ``python -m pyvised_backend serve | healthcheck | selftest``."""

import argparse
import logging
import sys
import urllib.request
from pathlib import Path

from pyvised_protocol import API_PREFIX, CONTAINER_PORT


def _serve(args: argparse.Namespace) -> int:
    import uvicorn

    import pyvised_backend
    import pyvised_protocol

    # watch the package sources (mounted by `just backend-dev`), not the working directory
    sources = [
        str(Path(str(module.__file__)).parent) for module in (pyvised_backend, pyvised_protocol)
    ]
    uvicorn.run(
        "pyvised_backend.server:create_app",
        factory=True,
        host=args.host,
        port=args.port,
        reload=args.reload,
        reload_dirs=sources if args.reload else None,
    )
    return 0


def _healthcheck(args: argparse.Namespace) -> int:
    url = f"http://127.0.0.1:{args.port}{API_PREFIX}/health"
    try:
        with urllib.request.urlopen(url, timeout=4) as response:
            return 0 if response.status == 200 else 1
    except OSError:
        return 1


def _selftest(args: argparse.Namespace) -> int:
    from .selftest import RESULT_MARKER, run_selftest

    result = run_selftest(keep=args.keep)
    print(RESULT_MARKER + result.model_dump_json(), flush=True)
    return 0 if result.ok else 1


def main(argv: list[str] | None = None) -> int:
    logging.basicConfig(level=logging.INFO, format="%(levelname)s %(name)s: %(message)s")
    parser = argparse.ArgumentParser(
        prog="python -m pyvised_backend", description="PyVised backend (OpenMC service)"
    )
    sub = parser.add_subparsers(dest="command", required=True)

    serve = sub.add_parser("serve", help="run the HTTP service")
    serve.add_argument("--host", default="127.0.0.1")
    serve.add_argument("--port", type=int, default=CONTAINER_PORT)
    serve.add_argument("--reload", action="store_true", help="restart on code changes")
    serve.set_defaults(func=_serve)

    health = sub.add_parser("healthcheck", help="exit 0 when the local service answers")
    health.add_argument("--port", type=int, default=CONTAINER_PORT)
    health.set_defaults(func=_healthcheck)

    selftest = sub.add_parser("selftest", help="build, render and run a tiny pin-cell model")
    selftest.add_argument("--keep", type=Path, help="keep the model and its results here")
    selftest.set_defaults(func=_selftest)

    args = parser.parse_args(argv)
    return args.func(args)


if __name__ == "__main__":
    sys.exit(main())
