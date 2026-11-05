"""wahoo-worker: run the FTMS service (`ftms ...`) or the CPS logger (`lsl ...`) through their existing CLIs."""
from __future__ import annotations

import sys
from typing import Sequence

USAGE = "usage: wahoo-worker {ftms|lsl} <arguments>"


def main(argv: Sequence[str] | None = None) -> int:
    args = list(sys.argv[1:] if argv is None else argv)
    if not args or args[0] not in ("ftms", "lsl"):
        print(USAGE, file=sys.stderr)
        return 2
    if args[0] == "ftms":
        from wahoo_lsl.ftms import cli
    else:
        from wahoo_lsl import cli
    return cli.main(args[1:])


if __name__ == "__main__":
    raise SystemExit(main())
