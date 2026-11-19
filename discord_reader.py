"""Read registered Discord guild channels using the v10 REST API."""

import asyncio
import math
import os
import re
from dataclasses import dataclass
from pathlib import Path
from urllib.parse import urlsplit

import registry
import httpx2 as httpx
from pydantic import BaseModel, ConfigDict, Field, field_validator, model_validator


CONFIG_FILE = Path(__file__).resolve().with_name("channel.url")
API = "https://discord.com/api/v10"
VERSION = "2.1.0"
PROJECT_URL = "https://github.com/moriyakeiichi/muse-discord-reader"
ID_PATTERN = r"^[1-9][0-9]{0,19}$"
MAX_ID = (1 << 64) - 1
MESSAGE_CHANNEL_TYPES = {0, 2, 5, 10, 11, 12, 13}


class ReadError(Exception):
    """A message that is safe to show to the user."""


def valid_id(value):
    if not isinstance(value, str) or re.fullmatch(ID_PATTERN, value) is None:
        return False
    return int(value) <= MAX_ID


@dataclass(frozen=True)
class Target:
    guild: str
    channel: str

    @property
    def link(self):
        return f"https://discord.com/channels/{self.guild}/{self.channel}"

    @classmethod
    def from_link(cls, link):
        hint = "対象チャンネルでコピーした https://discord.com/channels/サーバーID/チャンネルID 形式のリンクを指定してください。"
        try:
            url = urlsplit(link.strip())
        except ValueError:
            raise ReadError(hint) from None
        parts = url.path.strip("/").split("/")
        hosts = {"discord.com", "www.discord.com", "ptb.discord.com", "canary.discord.com"}
        if (url.scheme != "https" or url.netloc.lower() not in hosts
                or len(parts) not in (3, 4) or parts[0] != "channels"
                or not all(valid_id(part) for part in parts[1:])):
            raise ReadError(hint)
        return cls(parts[1], parts[2])

    @classmethod
    def configured(cls):
        try:
            return cls.from_link(CONFIG_FILE.read_text(encoding="utf-8"))
        except (OSError, UnicodeError):
            raise ReadError("チャンネル設定を読めません。muse-discord setup で対象リンクを登録してください。") from None


class PageRequest(BaseModel):
    model_config = ConfigDict(extra="forbid", strict=True)

    channel: str | None = Field(default=None, min_length=1, max_length=256)
    limit: int = Field(default=20, ge=1, le=100, description="取得件数（1〜100）")
    before: str | None = Field(default=None, pattern=ID_PATTERN, description="このメッセージIDより前。after と同時指定不可")
    after: str | None = Field(default=None, pattern=ID_PATTERN, description="このメッセージIDより後。before と同時指定不可")

    @field_validator("before", "after")
    @classmethod
    def check_id(cls, value):
        if value is not None and not valid_id(value):
            raise ValueError("DiscordのメッセージIDを文字列で指定してください。")
        return value

    @model_validator(mode="after")
    def check_direction(self):
        if self.before is not None and self.after is not None:
            raise ValueError("before と after は同時に指定できません。")
        return self


def authorization_header():
    # Muse replaces the whole custom_header value. A conventional MCP host
    # can instead supply a raw bot token, which needs Discord's Bot scheme.
    muse_header = "DISCORD_AUTHORIZATION_REF" in os.environ
    name = "DISCORD_AUTHORIZATION_REF" if muse_header else "DISCORD_BOT_TOKEN"
    value = os.environ.get(name, "")
    if not value or not value.isascii() or not value.isprintable() or any(c.isspace() for c in value):
        raise ReadError(f"{name} に有効な認証参照を渡してください。Museのヘッダー全体の代理参照は DISCORD_AUTHORIZATION_REF に設定します。")
    return value if muse_header else f"Bot {value}"


class DiscordCurlTransport(httpx.AsyncBaseTransport):
    """Use the VM's HTTPS client and proxy while keeping the credential off argv."""

    async def handle_async_request(self, request):
        headers = (
            f"Authorization: {request.headers['Authorization']}\n"
            f"User-Agent: {request.headers['User-Agent']}\n"
        ).encode("ascii")
        try:
            process = await asyncio.create_subprocess_exec(
                "curl", "-q", "--silent", "--proto", "=https", "--max-time", "25",
                "--header", "@-", "--write-out", "\n%{http_code}\n%header{retry-after}",
                "--url", str(request.url), stdin=asyncio.subprocess.PIPE,
                stdout=asyncio.subprocess.PIPE, stderr=asyncio.subprocess.DEVNULL,
            )
        except FileNotFoundError:
            raise ReadError("VMで curl を起動できません。curl のインストールと PATH を確認してください。") from None
        try:
            output, _ = await process.communicate(headers)
        except asyncio.CancelledError:
            if process.returncode is None:
                process.kill()
            await process.wait()
            raise
        if process.returncode:
            raise ReadError(f"DiscordへのHTTPS接続に失敗しました（curl {process.returncode}）。VMの通信許可とプロキシ設定を確認してください。")
        try:
            body, status, retry_after = output.rsplit(b"\n", 2)
            response_headers = {"retry-after": retry_after} if retry_after else {}
            return httpx.Response(int(status), headers=response_headers, content=body)
        except ValueError:
            raise ReadError("curl からDiscordのHTTPステータスを取得できませんでした。") from None


def retry_seconds(response):
    try:
        value = response.headers.get("retry-after")
        if value is None:
            value = response.json()["retry_after"]
        seconds = float(value)
        if math.isfinite(seconds) and seconds >= 0:
            return seconds
    except (KeyError, TypeError, ValueError):
        pass
    return None


async def get_discord(client, path, params=None):
    for attempt in range(2):
        try:
            response = await client.get(f"{API}{path}", params=params)
        except (httpx.RequestError, OSError):
            raise ReadError("Discordに接続できません。VMの通信許可、TLS設定、ネットワークを確認してください。") from None
        if response.status_code == 429:
            delay = retry_seconds(response)
            if attempt == 0 and delay is not None and delay <= 2:
                await asyncio.sleep(delay)
                continue
            when = f"{math.ceil(delay)}秒後に" if delay is not None else "少し時間を置いて"
            raise ReadError(f"Discordの回数制限です（429）。{when}再試行してください。")
        errors = {
            401: "認証に失敗しました（401）。専用BotのトークンとMuseの認証情報の割り当てを確認してください。",
            403: "閲覧が拒否されました（403）。Botの参加と表示・履歴権限を確認してください。ボイス・ステージでは「接続」、非公開スレッドでは参加も必要です。",
            404: "チャンネルが見つからないか、Botから見えません（404）。対象リンクとチャンネル権限を確認してください。",
        }
        if response.status_code != 200:
            raise ReadError(errors.get(response.status_code, f"Discordから HTTP {response.status_code} が返りました。"))
        try:
            return response.json()
        except ValueError:
            raise ReadError("Discordの応答がJSONではありません。接続先とVMの通信許可を確認してください。") from None


def object_list(value):
    if not isinstance(value, list) or not all(isinstance(item, dict) for item in value):
        raise ValueError("expected a list of objects")
    return value


def text_field(item, key):
    value = item.get(key) if isinstance(item, dict) else None
    return value if isinstance(value, str) else None


def named_items(value, key):
    names = [text_field(item, key) for item in object_list(value)]
    if any(name is None for name in names):
        raise ValueError("missing or invalid name")
    return names


def component_texts(components):
    for item in object_list(components):
        if item.get("type") == 10 and isinstance(item.get("content"), str):
            yield item["content"]
        if "components" in item:
            yield from component_texts(item["components"])


def project_embed(embed):
    result = {key: embed[key] for key in ("title", "description") if text_field(embed, key) is not None}
    if "fields" in embed:
        fields = []
        for item in object_list(embed["fields"]):
            name, value = text_field(item, "name"), text_field(item, "value")
            if name is None or value is None:
                raise ValueError("missing embed field text")
            field = {"name": name, "value": value}
            if isinstance(item.get("inline"), bool):
                field["inline"] = item["inline"]
            fields.append(field)
        result["fields"] = fields
    return result


def project_detail(field, value):
    if field == "attachments":
        return named_items(value, "filename")
    if field == "sticker_items":
        return named_items(value, "name")
    if field == "embeds":
        return [project_embed(embed) for embed in object_list(value)]
    if field == "components":
        return list(component_texts(value))
    if not isinstance(value, dict):  # poll
        raise ValueError("expected a poll object")
    return {
        "question": text_field(value.get("question"), "text"),
        "answers": [text_field(item.get("poll_media"), "text") for item in object_list(value.get("answers", []))],
    }


def project_message(row, target):
    if (not isinstance(row, dict) or row.get("channel_id") != target.channel
            or row.get("guild_id", target.guild) != target.guild or not valid_id(row.get("id"))):
        raise ReadError("設定したチャンネルと照合できないメッセージを受け取ったため、取得を中止しました。")
    author = row.get("author")
    message = {
        "id": row["id"],
        "author": text_field(row.get("member"), "nick") or text_field(author, "global_name") or text_field(author, "username"),
        "timestamp": text_field(row, "timestamp"),
        "content": text_field(row, "content") or "",
        "link": f"{target.link}/{row['id']}",
    }
    if text_field(row, "edited_timestamp"):
        message["edited_timestamp"] = row["edited_timestamp"]
    omitted = []
    for field, output in (("attachments", "attachments"), ("embeds", "embeds"),
                          ("components", "component_texts"), ("sticker_items", "stickers"), ("poll", "poll")):
        if row.get(field):
            try:
                detail = project_detail(field, row[field])
            except ValueError:
                omitted.append(field)
            else:
                if detail:
                    message[output] = detail
    if omitted:
        message["note"] = "Discordの応答形式を確認できず、一部を省略しました（" + ", ".join(omitted) + "）。"
    return message


async def read_page(client, target, request):
    path = f"/channels/{target.channel}"
    channel = await get_discord(client, path)
    if not isinstance(channel, dict) or channel.get("id") != target.channel or channel.get("guild_id") != target.guild:
        raise ReadError("DiscordのサーバーIDまたはチャンネルIDが設定と一致しません。対象リンクを確認してください。")
    if type(channel.get("type")) is not int or channel["type"] not in MESSAGE_CHANNEL_TYPES:
        raise ReadError("このチャンネル種別に直接の履歴はありません。フォーラム・メディアでは対象の投稿を開き、そのリンクを設定してください。")

    rows = await get_discord(client, f"{path}/messages", request.model_dump(exclude_none=True, exclude={"channel"}))
    if not isinstance(rows, list) or len(rows) > request.limit:
        raise ReadError("Discordの履歴の応答形式を確認できませんでした。")
    messages = [project_message(row, target) for row in rows]
    messages.sort(key=lambda message: int(message["id"]), reverse=True)
    page = {
        "channel": {"id": target.channel, "name": text_field(channel, "name"), "link": target.link},
        "count": len(messages),
        "messages": messages,
        "next_page": None,
    }
    if len(messages) == request.limit:
        if request.after is None:
            page["next_page"] = {"before": messages[-1]["id"], "limit": request.limit}
        else:
            page["next_page"] = {"after": messages[0]["id"], "limit": request.limit}
    if not messages:
        page["note"] = "この条件で取得できた投稿は0件です。該当する投稿がある場合は「メッセージ履歴を読む」権限を確認してください。"
    elif not any(msg["content"] for msg in messages):
        page["note"] = "このページの通常本文はすべて空です。添付・埋め込みだけの投稿などでも起きます。通常のテキスト投稿がある場合は Message Content Intent を確認してください。"
    if page["next_page"] is not None:
        page["next_page"]["channel"] = target.channel
    return page


def discord_client():
    headers = {
        "Authorization": authorization_header(),
        "User-Agent": f"DiscordBot ({PROJECT_URL}, {VERSION})",
    }
    return httpx.AsyncClient(headers=headers, transport=DiscordCurlTransport(), follow_redirects=False)


async def register_channel(link, alias=None):
    target = Target.from_link(link)
    if alias is not None and (not alias.strip() or len(alias) > 100 or not alias.isprintable()):
        raise ReadError("呼び名は1〜100文字の表示可能な名前にしてください。")
    async with discord_client() as client:
        page = await read_page(client, target, PageRequest(limit=1))
        guild = await get_discord(client, f"/guilds/{target.guild}")
    if not isinstance(guild, dict) or guild.get("id") != target.guild or not text_field(guild, "name") or not page["channel"]["name"]:
        raise ReadError("サーバー名またはチャンネル名を確認できませんでした。")
    registry.save(target, page["channel"]["name"], guild["name"], alias.strip() if alias else None)
    result = {"registered": target.link, "guild": guild["name"], "name": page["channel"]["name"]}
    if not page["count"]:
        result["note"] = page["note"]
    return result


async def read_configured_page(request):
    target = registry.select(request.channel)
    async with discord_client() as client:
        return await read_page(client, target, request)
