"""Entry points: ``python -m api_workbench serve`` and ``python -m api_workbench run case.json``."""

import sys

from . import runner, server

USAGE = "usage: python -m api_workbench {serve|run} [case.json]"


def main(argv=None):
    args = list(sys.argv[1:] if argv is None else argv)
    if not args:
        print(USAGE, file=sys.stderr)
        return 2
    command, rest = args[0], args[1:]
    if command == "serve":
        if rest:
            print(USAGE, file=sys.stderr)
            return 2
        return server.serve()
    if command == "run":
        if len(rest) != 1:
            print(USAGE, file=sys.stderr)
            return 2
        return runner.run(rest[0])
    print(f"unknown command: {command}\n{USAGE}", file=sys.stderr)
    return 2


if __name__ == "__main__":
    sys.exit(main())
