"""Offline regression and MCP interoperability checks. All Discord records are invented."""

import argparse
import asyncio
import json
import os
import sqlite3
import sys
import tempfile
import unittest
from concurrent.futures import ThreadPoolExecutor
from contextlib import closing
from pathlib import Path
from unittest.mock import AsyncMock, patch

import httpx2 as httpx
from mcp import Client, StdioServerParameters
from pydantic import ValidationError

import discord_reader as discord
import muse
import registry
from server import make_server, serve


TARGET = discord.Target("111111111111111111", "222222222222222222")
CHANNEL = {"id": TARGET.channel, "guild_id": TARGET.guild, "type": 0, "name": "テスト連絡"}
SECRET = "only-a-test-reference"
REAL_CLIENT = httpx.AsyncClient


def message(number, content="試験用の投稿"):
    return {
        "id": str(number), "channel_id": TARGET.channel, "author": {"username": "利用者"},
        "timestamp": "2026-01-02T03:04:05+00:00", "content": content,
    }


def discord_fixture(request):
    if request.method != "GET" or request.url.host != "discord.com" or request.url.scheme != "https":
        raise AssertionError("Unexpected Discord destination or method")
    if request.headers.get("Authorization") != "Bot " + SECRET:
        return httpx.Response(401)
    channel_path = f"/api/v10/channels/{TARGET.channel}"
    other = discord.Target("333333333333333333", "444444444444444444")
    if request.url.path in (f"/api/v10/guilds/{TARGET.guild}", f"/api/v10/guilds/{other.guild}"):
        guild = request.url.path.split("/")[-1]
        return httpx.Response(200, json={"id": guild, "name": "新サーバー" if guild == other.guild else "旧サーバー"})
    if request.url.path == f"/api/v10/channels/{other.channel}":
        return httpx.Response(200, json={**CHANNEL, "id": other.channel, "guild_id": other.guild})
    if request.url.path == f"/api/v10/channels/{other.channel}/messages":
        assert "channel" not in request.url.params
        return httpx.Response(200, json=[{**message(109), "channel_id": other.channel}])
    if request.url.path == "/api/v10/channels/555":
        return httpx.Response(403)
    if request.url.path == channel_path:
        return httpx.Response(200, json=CHANNEL)
    if request.url.path != channel_path + "/messages":
        raise AssertionError("Unexpected Discord path")
    params = request.url.params
    ids = [107, 106, 105, 104]
    if "before" in params:
        ids = [n for n in ids if n < int(params["before"])]
    if "after" in params:
        ids = sorted(n for n in ids if n > int(params["after"]))
    return httpx.Response(200, json=[message(n) for n in ids[:int(params["limit"])]])


async def fixture_server(config_file):
    # Only the test subprocess replaces the Discord HTTP transport. It still runs
    # the real server, config lookup, credential header, tool, and JSON responses.
    with patch.object(discord, "CONFIG_FILE", config_file), patch.object(
        discord, "DiscordCurlTransport", return_value=httpx.MockTransport(discord_fixture),
    ):
        await serve(make_server())


class SettingsTests(unittest.TestCase):
    def test_channel_and_message_links(self):
        for link in (TARGET.link, TARGET.link + "/", TARGET.link + "/123?jump=1"):
            self.assertEqual(discord.Target.from_link(link), TARGET)

    def test_reject_non_guild_links_and_invalid_ids(self):
        for url in ("https://discord.gg/test", TARGET.link.replace("https:", "http:"),
                    TARGET.link.replace("discord.com", "discord.com.other.example"),
                    TARGET.link.replace(TARGET.guild, "@me"), TARGET.link.replace(TARGET.channel, "０１２"),
                    TARGET.link.replace(TARGET.channel, "18446744073709551616"), TARGET.link + "/../anything"):
            with self.subTest(url=url), self.assertRaises(discord.ReadError):
                discord.Target.from_link(url)

    def test_request_bounds_and_fixed_target(self):
        for args in ({"limit": 0}, {"limit": 101}, {"limit": True}, {"limit": 1.5},
                     {"channel_id": "123"}, {"guild_id": "12"}, {"url": "https://example.org"},
                     {"before": "123", "after": "124"}, {"before": 123}, {"after": "1/2"},
                     {"after": str(1 << 64)}, {"after": "١٢٣"}):
            with self.subTest(args=args), self.assertRaises(ValidationError):
                discord.PageRequest.model_validate(args)
        self.assertEqual(discord.PageRequest(limit=100).limit, 100)

    def test_missing_and_malformed_credentials(self):
        for value in ("", "Bot secret", "test\nref"):
            with self.subTest(value=value), patch.dict(os.environ, {"DISCORD_BOT_TOKEN": value}):
                with self.assertRaises(discord.ReadError):
                    discord.authorization_header()

    def test_muse_full_header_reference_is_forwarded_verbatim(self):
        with patch.dict(os.environ, {"DISCORD_AUTHORIZATION_REF": "opaque-muse-reference", "DISCORD_BOT_TOKEN": SECRET}):
            self.assertEqual(discord.authorization_header(), "opaque-muse-reference")
            self.assertEqual(muse.server_process().env["DISCORD_AUTHORIZATION_REF"], "opaque-muse-reference")
            os.environ["DISCORD_AUTHORIZATION_REF"] = "Bot opaque-muse-reference"
            with self.assertRaises(discord.ReadError):
                discord.authorization_header()
        with patch.dict(os.environ, {"DISCORD_BOT_TOKEN": SECRET}, clear=True):
            self.assertEqual(discord.authorization_header(), "Bot " + SECRET)

    def test_only_connector_and_network_variables_passed(self):
        with patch.dict(os.environ, {"DISCORD_BOT_TOKEN": SECRET, "UNRELATED_TEST_SECRET": "not-for-child",
                                     "https_proxy": "http://example.invalid:8080"}):
            settings = muse.server_process()
            self.assertEqual(settings.env["DISCORD_BOT_TOKEN"], SECRET)
            self.assertIn("https_proxy", settings.env)
            self.assertNotIn("UNRELATED_TEST_SECRET", settings.env)
            self.assertNotIn(SECRET, settings.args)


class DiscordTests(unittest.IsolatedAsyncioTestCase):
    async def fetch(self, handler, **args):
        async with REAL_CLIENT(transport=httpx.MockTransport(handler), headers={"Authorization": "Bot " + SECRET}) as client:
            return await discord.read_page(client, TARGET, discord.PageRequest(**args))

    async def test_vm_curl_receives_credential_only_on_stdin_and_preserves_headers(self):
        process = AsyncMock()
        process.returncode = 0
        process.communicate.return_value = (b'{"retry_after": 4}\n429\n3.2', None)
        headers = {"Authorization": "Bot " + SECRET, "User-Agent": "MuseChannelReader"}
        url = f"https://discord.com/api/v10/channels/{TARGET.channel}/messages?limit=1"
        with patch.object(asyncio, "create_subprocess_exec", return_value=process) as start:
            async with REAL_CLIENT(headers=headers, transport=discord.DiscordCurlTransport()) as client:
                response = await client.get(url)
        argv = start.await_args.args
        self.assertEqual(argv[:2], ("curl", "-q"))
        self.assertIn(url, argv)
        self.assertEqual(argv[argv.index("--header") + 1], "@-")
        self.assertNotIn(SECRET, repr(argv))
        self.assertNotIn("--location", argv)
        self.assertNotIn("--insecure", argv)
        process.communicate.assert_awaited_once_with(f"Authorization: Bot {SECRET}\nUser-Agent: MuseChannelReader\n".encode())
        self.assertEqual((response.status_code, discord.retry_seconds(response)), (429, 3.2))
        self.assertEqual(response.json(), {"retry_after": 4})

    async def test_vm_curl_failure_returns_only_its_exit_code(self):
        process = AsyncMock()
        process.returncode = 28
        process.communicate.return_value = (SECRET.encode(), None)
        with patch.object(asyncio, "create_subprocess_exec", return_value=process):
            with patch.dict(os.environ, {"DISCORD_BOT_TOKEN": SECRET}):
                with patch("registry.select", return_value=TARGET), self.assertRaisesRegex(discord.ReadError, "curl 28") as failure:
                    await discord.read_configured_page(discord.PageRequest(limit=1))
        self.assertNotIn(SECRET, str(failure.exception))

    async def test_backward_and_forward_pagination(self):
        page = await self.fetch(discord_fixture, limit=2)
        self.assertEqual([m["id"] for m in page["messages"]], ["107", "106"])
        older = await self.fetch(discord_fixture, **page["next_page"])
        self.assertEqual([m["id"] for m in older["messages"]], ["105", "104"])
        after = await self.fetch(discord_fixture, limit=2, after="104")
        self.assertEqual([m["id"] for m in after["messages"]], ["106", "105"])
        newer = await self.fetch(discord_fixture, **after["next_page"])
        self.assertEqual([m["id"] for m in newer["messages"]], ["107"])
        self.assertIsNone(newer["next_page"])

    async def test_wrong_guild_channel_or_container_stops_before_history(self):
        for change in ({"guild_id": "999"}, {"guild_id": None}, {"id": "123"}, {"type": 15}):
            urls = []
            def respond(request):
                urls.append(request.url.path)
                return httpx.Response(200, json={**CHANNEL, **change})
            with self.subTest(change=change), self.assertRaises(discord.ReadError):
                await self.fetch(respond)
            self.assertEqual(urls, [f"/api/v10/channels/{TARGET.channel}"])

    async def test_mixed_response_never_returns_partial_data(self):
        for change in ({"channel_id": "999"}, {"guild_id": "999"}, {"id": "not-an-id"}):
            data = [message(15), {**message(14), **change}]
            def respond(request):
                return httpx.Response(200, json=data if request.url.path.endswith("/messages") else CHANNEL)
            with self.subTest(change=change), self.assertRaises(discord.ReadError):
                await self.fetch(respond)

    async def test_source_link_text_and_attachment_boundary(self):
        row = {
            **message(107, "一行目\n二行目"), "member": {"nick": "表示名"},
            "attachments": [{"filename": "原稿.pdf", "url": "https://example.invalid/private"}],
            "embeds": [{"title": "議題", "url": "https://example.invalid/ignored",
                       "fields": [{"name": "場所", "value": "会議室", "inline": True, "extra": "https://example.invalid/ignored"}]}],
            "components": [{"type": 17, "components": [{"type": 10, "content": "補足"}]}],
            "referenced_message": {"channel_id": "999", "content": "別チャンネルの投稿"},
            "message_snapshots": [{"message": {"content": "転送元の投稿"}}],
        }
        def respond(request):
            return httpx.Response(200, json=[row] if request.url.path.endswith("/messages") else CHANNEL)
        page = await self.fetch(respond)
        item = page["messages"][0]
        self.assertEqual(item["link"], TARGET.link + "/107")
        self.assertEqual(item["content"], "一行目\n二行目")
        self.assertEqual(item["author"], "表示名")
        self.assertEqual(item["attachments"], ["原稿.pdf"])
        self.assertEqual(item["embeds"][0]["fields"], [{"name": "場所", "value": "会議室", "inline": True}])
        self.assertEqual(item["component_texts"], ["補足"])
        serialized = json.dumps(page, ensure_ascii=False)
        for excluded in ("別チャンネルの投稿", "転送元の投稿", "example.invalid", SECRET):
            self.assertNotIn(excluded, serialized)

    async def test_empty_and_non_text_samples_have_distinct_diagnostics(self):
        for rows, status, code in (([], "no_messages", 2), ([message(104, "")], "no_text_in_sample", 2),
                                  ([{**message(104, ""), "attachments": [{"filename": "画像.png"}]}],
                                   "other_content_visible", 0)):
            def respond(request):
                return httpx.Response(200, json=rows if request.url.path.endswith("/messages") else CHANNEL)
            report, actual_code = muse.check_sample(await self.fetch(respond))
            self.assertEqual((report["status"], actual_code), (status, code))
            self.assertIn("note", report)

    async def test_incomplete_optional_fields_preserve_verified_text(self):
        row = {**message(107), "attachments": [{"url": "https://example.invalid/private"}],
               "embeds": [None], "components": [{"type": 17, "components": "invalid"}],
               "sticker_items": [{"name": "見本"}], "poll": {"answers": {}}, "member": "invalid"}
        def respond(request):
            return httpx.Response(200, json=[row, message(106)] if request.url.path.endswith("/messages") else CHANNEL)
        page = await self.fetch(respond)
        first, second = page["messages"]
        self.assertEqual((first["content"], first["author"], first["stickers"]), ("試験用の投稿", "利用者", ["見本"]))
        self.assertEqual(second["id"], "106")
        for field in ("attachments", "embeds", "components", "poll"):
            self.assertIn(field, first["note"])
            self.assertNotIn(field, first)
        self.assertNotIn("example.invalid", json.dumps(page))

    async def test_user_agent_matches_project_and_mcp_version(self):
        server_version = make_server().create_initialization_options().server_version
        with patch.dict(os.environ, {"DISCORD_BOT_TOKEN": SECRET}):
            async with discord.discord_client() as client:
                self.assertEqual(client.headers["User-Agent"], f"DiscordBot ({discord.PROJECT_URL}, {server_version})")

    async def test_http_errors_do_not_forward_bodies_or_follow_redirects(self):
        for status in (301, 401, 403, 404, 500, 503):
            calls = []
            def respond(request):
                calls.append(str(request.url))
                return httpx.Response(status, text=SECRET, headers={"Location": "https://example.invalid/private"})
            with self.subTest(status=status), self.assertRaises(discord.ReadError) as failure:
                await self.fetch(respond)
            self.assertIn(str(status), str(failure.exception))
            self.assertNotIn(SECRET, str(failure.exception))
            self.assertEqual(len(calls), 1)

    async def test_rate_limits_respect_server_wait_and_retry_once(self):
        responses = [
            httpx.Response(429, json={"retry_after": 0.25}),
            httpx.Response(200, json=CHANNEL),
            httpx.Response(200, json=[message(104)]),
        ]
        with patch("discord_reader.asyncio.sleep", new_callable=AsyncMock) as sleep:
            page = await self.fetch(lambda _: responses.pop(0))
            sleep.assert_awaited_once_with(0.25)
        self.assertEqual(page["count"], 1)
        for header, expected in (("3.1", "4秒後"), ("not-a-number", "少し時間")):
            with self.assertRaises(discord.ReadError) as failure:
                await self.fetch(lambda _: httpx.Response(429, headers={"Retry-After": header}))
            self.assertIn(expected, str(failure.exception))

    async def test_malformed_response_and_network_failure(self):
        def timeout(request):
            raise httpx.ReadTimeout(SECRET, request=request)
        for handler in (timeout, lambda _: httpx.Response(200, content=b"not-json")):
            with self.assertRaises(discord.ReadError) as failure:
                await self.fetch(handler)
            self.assertNotIn(SECRET, str(failure.exception))


class RegistryTests(unittest.TestCase):
    def setUp(self):
        temp = tempfile.TemporaryDirectory()
        self.addCleanup(temp.cleanup)
        self.config = Path(temp.name) / "channel.url"
        setting = patch.object(discord, "CONFIG_FILE", self.config)
        setting.start()
        self.addCleanup(setting.stop)

    def test_old_database_and_link_migrate_without_losing_alias_or_default(self):
        other = discord.Target("3", "4")
        self.config.write_text(TARGET.link, encoding="utf-8")
        with closing(sqlite3.connect(registry.db_path())) as db, db:
            db.execute("CREATE TABLE channels (id TEXT PRIMARY KEY, guild TEXT, name TEXT, guild_name TEXT, alias TEXT)")
            db.execute("INSERT INTO channels VALUES ('4','3','会議','試験','既存の呼び名')")
        before = registry.entries()
        self.assertEqual([r["id"] for r in before], [TARGET.channel, "4"])
        self.assertEqual(registry.select(None), TARGET)
        registry.set_default("既存の呼び名")
        self.assertEqual(registry.select(None), other)
        self.assertEqual([r["id"] for r in registry.entries()], [r["id"] for r in before])
        registry.remove(other.channel)
        self.assertEqual(registry.select(None), TARGET)
        registry.remove(TARGET.channel)
        self.assertEqual(registry.entries(), [])
        self.assertEqual(self.config.read_text(), TARGET.link)
        registry.save(other, "会議", "試験", None)
        self.assertEqual(registry.select(None), other)

    def test_concurrent_first_add_preserves_every_channel_and_one_default(self):
        def add(number):
            registry.save(discord.Target("1", str(number)), str(number), "試験", None)
        with ThreadPoolExecutor(max_workers=4) as workers:
            list(workers.map(add, range(10, 18)))
        rows = registry.entries()
        self.assertEqual({row["id"] for row in rows}, {str(i) for i in range(10, 18)})
        self.assertEqual(sum(row["default"] for row in rows), 1)


class ProtocolTests(unittest.IsolatedAsyncioTestCase):
    def setUp(self):
        self.temp = tempfile.TemporaryDirectory()
        self.addCleanup(self.temp.cleanup)
        config = Path(self.temp.name) / "channel.url"
        config.write_text(TARGET.link, encoding="utf-8")
        self.process = StdioServerParameters(
            command=sys.executable, args=["-B", str(Path(__file__).resolve()), "--fixture-server", str(config)],
            env={"DISCORD_BOT_TOKEN": SECRET},
        )

    async def test_registry_across_servers_and_restarts(self):
        other = discord.Target("333333333333333333", "444444444444444444")
        async with Client(self.process) as client:
            legacy = await client.call_tool("list_discord_channels", {})
            self.assertTrue(legacy.structured_content["channels"][0]["default"])
            for target, alias in ((TARGET, "旧連絡"), (other, "新連絡"), (other, "新連絡")):
                added = await client.call_tool("add_discord_channel", {"link": target.link, "alias": alias})
                self.assertFalse(added.is_error, str(added.content))
            denied = await client.call_tool("add_discord_channel", {"link": other.link.replace(other.channel, "555")})
            self.assertTrue(denied.is_error)
        async with Client(self.process) as client:
            rows = (await client.call_tool("list_discord_channels", {})).structured_content["channels"]
            self.assertEqual([(r["id"], r["default"]) for r in rows], [(TARGET.channel, True), (other.channel, False)])
            ambiguous = await client.call_tool(muse.TOOL, {"channel": "テスト連絡"})
            self.assertTrue(ambiguous.is_error)
            for name in ("新連絡", "新サーバー/#テスト連絡", other.channel):
                page = (await client.call_tool(muse.TOOL, {"channel": name, "limit": 1})).structured_content
                self.assertEqual(page["channel"]["id"], other.channel)
                self.assertEqual(page["next_page"]["channel"], other.channel)
            self.assertEqual((await client.call_tool(muse.TOOL, {})).structured_content["channel"]["id"], TARGET.channel)
            self.assertTrue((await client.call_tool(muse.TOOL, {"channel": "999"})).is_error)

    async def test_wire_discovery_call_and_legacy_handshake(self):
        for mode in ("auto", "legacy"):
            async with Client(self.process, mode=mode, read_timeout_seconds=10) as client:
                listing = await client.list_tools()
                self.assertEqual([tool.name for tool in listing.tools], [
                    muse.TOOL, "list_discord_channels", "add_discord_channel", "set_default_discord_channel", "remove_discord_channel",
                ])
                tool = listing.tools[0]
                self.assertTrue(tool.annotations.read_only_hint)
                self.assertFalse(tool.input_schema["additionalProperties"])
                reply = await client.call_tool(muse.TOOL, {"limit": 1})
                self.assertFalse(reply.is_error)
                self.assertEqual(reply.structured_content["messages"][0]["content"], "試験用の投稿")
                self.assertEqual(json.loads(reply.content[0].text), reply.structured_content)
                for name, args in ((muse.TOOL, {"channel_id": "999"}), ("send_message", {})):
                    error = await client.call_tool(name, args)
                    self.assertTrue(error.is_error)

    async def test_default_and_removal_by_name_across_processes(self):
        other = discord.Target("333333333333333333", "444444444444444444")
        async with Client(self.process) as client:
            for target, alias in ((TARGET, "最初"), (other, "次")):
                reply = await client.call_tool("add_discord_channel", {"link": target.link, "alias": alias})
                self.assertFalse(reply.is_error)
            for tool in ("set_default_discord_channel", "remove_discord_channel"):
                for args in ({}, {"channel": "未登録"}, {"channel": "テスト連絡"}):
                    self.assertTrue((await client.call_tool(tool, args)).is_error)
        # Both management commands work offline, even with a non-working Discord credential.
        self.process.env["DISCORD_BOT_TOKEN"] = "invalid-test-reference"
        selected, code = await muse.invoke(argparse.Namespace(command="default", channel="次"), self.process)
        self.assertEqual((selected["default"], code), (other.link, 0))
        removed, code = await muse.invoke(argparse.Namespace(command="remove", channel="最初"), self.process)
        self.assertEqual((removed["removed"], code), (TARGET.link, 0))
        rows, _ = await muse.invoke(argparse.Namespace(command="list"), self.process)
        self.assertEqual([(r["id"], r["default"]) for r in rows["channels"]], [(other.channel, True)])
        self.process.env["DISCORD_BOT_TOKEN"] = SECRET
        async with Client(self.process) as client:
            self.assertTrue((await client.call_tool(muse.TOOL, {"channel": TARGET.channel})).is_error)
            page = (await client.call_tool(muse.TOOL, {"limit": 1})).structured_content
            self.assertEqual(page["channel"]["id"], other.channel)

    async def test_muse_client_read_check_and_safe_failure(self):
        report, code = await muse.invoke(argparse.Namespace(command="check"), self.process)
        self.assertEqual((report["status"], code), ("text_visible", 0))
        self.assertNotIn("試験用の投稿", json.dumps(report, ensure_ascii=False))
        page, code = await muse.invoke(argparse.Namespace(command="read", limit=1, before="106"), self.process)
        self.assertEqual((page["messages"][0]["id"], code), ("105", 0))
        self.process.env["DISCORD_BOT_TOKEN"] = "invalid-test-reference"
        with self.assertRaisesRegex(discord.ReadError, "401"):
            await muse.invoke(argparse.Namespace(command="check"), self.process)


if __name__ == "__main__":
    if len(sys.argv) == 3 and sys.argv[1] == "--fixture-server":
        asyncio.run(fixture_server(Path(sys.argv[2])))
    else:
        unittest.main()
