"""命令行入口：python -m api_workbench serve | run case.json"""

from __future__ import annotations

import argparse

from .runner import run_case
from .server import serve


def main(argv: list[str] | None = None) -> int:
    parser = argparse.ArgumentParser(
        prog="api_workbench",
        description="本地 HTTP 接口回归工具（单用例）",
    )
    subparsers = parser.add_subparsers(dest="command", required=True)

    subparsers.add_parser("serve", help="启动 127.0.0.1:8765 示例服务")

    run_parser = subparsers.add_parser("run", help="执行一个 JSON 用例")
    run_parser.add_argument("case_file", help="用例 JSON 文件路径")

    args = parser.parse_args(argv)

    if args.command == "serve":
        return serve()
    if args.command == "run":
        return run_case(args.case_file)
    parser.error(f"未知子命令: {args.command}")  # 理论上不可达
    return 2
