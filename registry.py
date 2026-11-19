"""Per-installation channel registry, including the original single-channel setting."""

import sqlite3
from contextlib import closing
from unicodedata import normalize

import discord_reader as discord


def key(text):
    return normalize("NFKC", text).strip().removeprefix("#").casefold()


def db_path():
    return discord.CONFIG_FILE.with_name("channels.sqlite3")


def save(target, name, guild_name, alias):
    path = db_path()
    path.touch(mode=0o600, exist_ok=True)
    with closing(sqlite3.connect(path, timeout=10)) as db, db:
        db.execute("""CREATE TABLE IF NOT EXISTS channels
                      (id TEXT PRIMARY KEY, guild TEXT NOT NULL, name TEXT NOT NULL,
                       guild_name TEXT NOT NULL, alias TEXT NOT NULL)""")
        changed = db.execute("""INSERT INTO channels VALUES (?, ?, ?, ?, ?)
            ON CONFLICT(id) DO UPDATE SET name=excluded.name, guild_name=excluded.guild_name,
            alias=CASE WHEN excluded.alias='' THEN channels.alias ELSE excluded.alias END
            WHERE channels.guild=excluded.guild""", (target.channel, target.guild, name, guild_name, alias or ""))
        if not changed.rowcount:
            raise discord.ReadError("登録済みのサーバーIDと一致しません。")


def entries():
    legacy = discord.Target.configured() if discord.CONFIG_FILE.exists() else None
    rows = []
    if db_path().exists():
        with closing(sqlite3.connect(db_path(), timeout=10)) as db:
            # Start-up can race the first creator before its schema is committed.
            if db.execute("SELECT 1 FROM sqlite_master WHERE name='channels'").fetchone():
                rows = db.execute("SELECT id,guild,name,guild_name,alias FROM channels ORDER BY rowid").fetchall()
    if legacy and not any(row[:2] == (legacy.channel, legacy.guild) for row in rows):
        rows.insert(0, (legacy.channel, legacy.guild, legacy.channel, legacy.guild, ""))
    default = legacy.channel if legacy else (rows[0][0] if rows else None)
    return [dict(id=i, guild_id=g, name=n, guild_name=gn, alias=a, default=i==default,
                 link=discord.Target(g,i).link) for i,g,n,gn,a in rows]


def select(channel):
    rows = entries()
    if channel is None:
        matches = [row for row in rows if row["default"]]
    else:
        needle = key(channel)
        exact_id = next((row for row in rows if row["id"] == needle), None)
        if exact_id:
            return discord.Target(exact_id["guild_id"], exact_id["id"])
        matches = [row for row in rows if needle and needle in {
            key(s) for s in (row["id"], row["alias"], row["name"], row["link"],
                             f'{row["guild_name"]}/#{row["name"]}') if s}]
    if len(matches) == 1:
        return discord.Target(matches[0]["guild_id"], matches[0]["id"])
    if matches:
        choices = ", ".join(f'{r["guild_name"]}/#{r["name"]} ({r["id"]})' for r in matches)
        raise discord.ReadError("同じ名前の候補があります。チャンネルIDで選んでください。 " + choices)
    raise discord.ReadError("対象の登録がありません。list で一覧を確認するか、チャンネルリンクで追加してください。")
