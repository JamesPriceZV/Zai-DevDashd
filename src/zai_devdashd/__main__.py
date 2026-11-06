"""Console entry: zai-devdashd --config <file>."""

from __future__ import annotations

import argparse
import os
import sys
from pathlib import Path


def main(argv: list | None = None) -> int:
    parser = argparse.ArgumentParser(prog="zai-devdashd")
    parser.add_argument(
        "--config",
        default=os.environ.get("ZAI_DEVDASHD_CONFIG") or "",
        help="Path to config.toml. The default is ~/.config/zai-devdashd/config.toml.",
    )
    parser.add_argument("--once", action="store_true", help="Print one JSON snapshot and exit.")
    parser.add_argument("--smoke", action="store_true", help="Same as --once.")
    args = parser.parse_args(argv)
    from zai_devdashd import config as site

    path = Path(args.config).expanduser() if args.config else site.default_config_path()
    site.load(path)
    if os.environ.get("ZAI_DEVDASHD_SELFTEST") == "1":
        from zai_devdashd.aggregator import self_test

        self_test()
        return 0
    if args.once or args.smoke:
        import json

        from zai_devdashd.aggregator import ENGINE
        from zai_devdashd.probes import redact

        ENGINE.poll_local()
        snap = redact(ENGINE.build())
        sys.stdout.write(json.dumps({
            "server_time": snap.get("server_time"),
            "active": snap.get("active"),
            "overview": {"tiles": [
                {"name": tile.get("name"), "job": tile.get("job"), "status": tile.get("status")}
                for tile in ((snap.get("overview") or {}).get("tiles") or [])
            ]},
        }, default=str) + "\n")
        return 0
    from zai_devdashd.server import serve

    serve()
    return 0


if __name__ == "__main__":
    sys.exit(main())
