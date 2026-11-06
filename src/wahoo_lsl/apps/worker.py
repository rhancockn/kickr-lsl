"""wahoo-worker: run the FTMS service (`ftms ...`), the CPS logger (`lsl ...`) or both in one process (`server ...`)."""
from __future__ import annotations

import sys
from typing import Sequence

USAGE = "usage: wahoo-worker {ftms|lsl|server} <arguments>"


def main(argv: Sequence[str] | None = None) -> int:
    args = list(sys.argv[1:] if argv is None else argv)
    if not args or args[0] not in ("ftms", "lsl", "server"):
        print(USAGE, file=sys.stderr)
        return 2
    if args[0] == "ftms":
        from wahoo_lsl.ftms import cli
    elif args[0] == "server":
        from wahoo_lsl.apps import server_worker as cli
    else:
        from wahoo_lsl import cli
    return cli.main(args[1:])


if __name__ == "__main__":
    raise SystemExit(main())
