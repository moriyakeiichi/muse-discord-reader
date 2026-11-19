"""Muse VM command-line client for the co-located stdio MCP server."""

import argparse
import asyncio
import json
import os
import sys
import registry
from pathlib import Path

from mcp import Client, StdioServerParameters

from discord_reader import ReadError


ROOT = Path(__file__).resolve().parent
TOOL = "read_discord_messages"
PASSTHROUGH = (
    "DISCORD_AUTHORIZATION_REF", "DISCORD_BOT_TOKEN", "HTTPS_PROXY", "HTTP_PROXY", "ALL_PROXY", "NO_PROXY",
    "https_proxy", "http_proxy", "all_proxy", "no_proxy", "SSL_CERT_FILE", "SSL_CERT_DIR", "CURL_CA_BUNDLE",
)


def server_process():
    return StdioServerParameters(
        command=sys.executable,
        args=["-B", str(ROOT / "server.py")],
        cwd=ROOT,
        env={key: os.environ[key] for key in PASSTHROUGH if key in os.environ},
    )


def check_sample(page):
    sample = next((row for row in page["messages"] if row["content"]), None)
    if sample:
        status = "text_visible"
    else:
        other_fields = ("attachments", "embeds", "component_texts", "stickers", "poll")
        sample = next((row for row in page["messages"] if any(row.get(field) for field in other_fields)), None)
        status = "other_content_visible" if sample else ("no_text_in_sample" if page["count"] else "no_messages")
    report = {
        "status": status, "channel": page["channel"], "checked_messages": page["count"],
        "sample_link": sample["link"] if sample else None,
    }
    if "note" in page:
        report["note"] = page["note"]
    return report, 0 if sample is not None else 2


async def invoke(args, process):
    async with Client(process, read_timeout_seconds=85) as client:
        if args.command == "tools":
            listing = await client.list_tools()
            return {"tools": [tool.name for tool in listing.tools], "protocol": client.protocol_version}, 0
        if args.command == "list":
            result = await client.call_tool("list_discord_channels", {})
        elif args.command == "add":
            result = await client.call_tool("add_discord_channel", {"link": args.url, "alias": args.alias})
        elif args.command in ("default", "remove"):
            name = "set_default_discord_channel" if args.command == "default" else "remove_discord_channel"
            result = await client.call_tool(name, {"channel": args.channel})
        else:
            arguments = {"limit": 5 if args.command == "check" else args.limit}
            for field in ("before", "after", "channel"):
                value = getattr(args, field, None)
                if value is not None:
                    arguments[field] = value
            result = await client.call_tool(TOOL, arguments)
    if result.is_error:
        message = next((block.text for block in result.content if block.type == "text"), "MCPの読み取りに失敗しました。")
        raise ReadError(message)
    if not isinstance(result.structured_content, dict):
        raise ReadError("MCPサーバーから履歴の応答を受け取れませんでした。")
    if args.command == "check":
        return check_sample(result.structured_content)
    return result.structured_content, 0


def main():
    parser = argparse.ArgumentParser(description="Muse用・Discordチャンネル読み取りMCP")
    commands = parser.add_subparsers(dest="command", required=True)
    configure = commands.add_parser("configure", help="リンクで登録。省略時は一覧")
    configure.add_argument("url", nargs="?", help="省略時は保存済みのチャンネルを確認")
    commands.add_parser("tools", help="MCP接続と公開ツールを確認")
    add = commands.add_parser("add", help="リンクでチャンネルを追加")
    add.add_argument("url")
    add.add_argument("--alias")
    commands.add_parser("list", help="登録済み一覧")
    default = commands.add_parser("default", help="既定の登録チャンネルを選ぶ")
    default.add_argument("channel", help="登録名または一覧のチャンネルID")
    remove = commands.add_parser("remove", help="このVMの一覧から登録を解除")
    remove.add_argument("channel", help="登録名または一覧のチャンネルID")
    check = commands.add_parser("check", help="最新5件で本文が読めるかを確認。本文自体は表示しません")
    read = commands.add_parser("read", help="指定チャンネルの履歴を取得")
    check.add_argument("--channel")
    read.add_argument("--channel")
    read.add_argument("--limit", type=int, default=20)
    direction = read.add_mutually_exclusive_group()
    direction.add_argument("--before")
    direction.add_argument("--after")
    args = parser.parse_args()
    try:
        if args.command == "configure":
            if args.url:
                args.command, args.alias = "add", None
                payload, status = asyncio.run(invoke(args, server_process()))
            else:
                payload, status = {"channels": registry.entries()}, 0
        else:
            payload, status = asyncio.run(invoke(args, server_process()))
    except ReadError as exc:
        print(str(exc), file=sys.stderr)
        return 1
    except (OSError, ValueError):
        print("設定ファイルまたはMCPプロセスを開けませんでした。配置先と実行環境を確認してください。", file=sys.stderr)
        return 1
    except Exception:
        # Transport exception groups can include subprocess details. Keep the CLI output credential-free.
        print("MCPの通信を完了できませんでした。muse-discord tools で起動状態を確認してください。", file=sys.stderr)
        return 1
    print(json.dumps(payload, ensure_ascii=False, indent=2))
    return status


if __name__ == "__main__":
    raise SystemExit(main())
