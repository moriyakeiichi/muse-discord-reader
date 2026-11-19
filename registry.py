"""Per-installation channel registry, including the original single-channel setting."""

import sqlite3
from contextlib import closing, contextmanager
from unicodedata import normalize

import discord_reader as discord


def key(text):
    return normalize("NFKC", text).strip().removeprefix("#").casefold()


def db_path():
    return discord.CONFIG_FILE.with_name("channels.sqlite3")


def first_id(db):
    row = db.execute("SELECT id FROM channels ORDER BY rowid LIMIT 1").fetchone()
    return row[0] if row else None


def legacy_target():
    return discord.Target.configured() if discord.CONFIG_FILE.exists() else None


@contextmanager
def writable_registry():
    path = db_path()
    path.touch(mode=0o600, exist_ok=True)
    with closing(sqlite3.connect(path, timeout=10)) as db, db:
        db.execute("BEGIN IMMEDIATE")
        db.execute("""CREATE TABLE IF NOT EXISTS channels
                      (id TEXT PRIMARY KEY, guild TEXT NOT NULL, name TEXT NOT NULL,
                       guild_name TEXT NOT NULL, alias TEXT NOT NULL)""")
        db.execute("""CREATE TABLE IF NOT EXISTS preferences
                      (singleton INTEGER PRIMARY KEY CHECK (singleton=1), default_id TEXT)""")
        if db.execute("SELECT 1 FROM preferences").fetchone() is None:
            # Import channel.url once. Removing its registration must survive a restart.
            legacy = legacy_target()
            if legacy:
                db.execute("""INSERT OR IGNORE INTO channels (rowid,id,guild,name,guild_name,alias)
                              SELECT COALESCE(MIN(rowid), 1)-1, ?, ?, ?, ?, '' FROM channels""",
                           (legacy.channel, legacy.guild, legacy.channel, legacy.guild))
                if db.execute("SELECT guild FROM channels WHERE id=?", (legacy.channel,)).fetchone() != (legacy.guild,):
                    raise discord.ReadError("旧設定と登録一覧のサーバーIDが一致しません。")
            default = legacy.channel if legacy else first_id(db)
            db.execute("INSERT INTO preferences (singleton,default_id) VALUES (1,?)", (default,))
        yield db


def save(target, name, guild_name, alias):
    with writable_registry() as db:
        changed = db.execute("""INSERT INTO channels (id,guild,name,guild_name,alias) VALUES (?, ?, ?, ?, ?)
            ON CONFLICT(id) DO UPDATE SET name=excluded.name, guild_name=excluded.guild_name,
            alias=CASE WHEN excluded.alias='' THEN channels.alias ELSE excluded.alias END
            WHERE channels.guild=excluded.guild""", (target.channel, target.guild, name, guild_name, alias or ""))
        if not changed.rowcount:
            raise discord.ReadError("登録済みのサーバーIDと一致しません。")
        db.execute("UPDATE preferences SET default_id=? WHERE default_id IS NULL", (target.channel,))


def channel_rows(db):
    return db.execute("SELECT id,guild,name,guild_name,alias FROM channels ORDER BY rowid").fetchall()


def describe(rows, default):
    return [dict(id=i, guild_id=g, name=n, guild_name=gn, alias=a, default=i == default,
                 link=discord.Target(g, i).link) for i, g, n, gn, a in rows]


def entries():
    rows = []
    if db_path().exists():
        with closing(sqlite3.connect(db_path(), timeout=10)) as db:
            db.execute("BEGIN")
            tables = {name for name, in db.execute("SELECT name FROM sqlite_master WHERE type='table'")}
            if "channels" in tables:
                rows = channel_rows(db)
            if "preferences" in tables:
                default, = db.execute("SELECT default_id FROM preferences").fetchone()
                return describe(rows, default)
    # Before the first write, keep older installations readable without modifying them.
    legacy = legacy_target()
    if legacy:
        saved = next((row for row in rows if row[0] == legacy.channel), None)
        if saved is None:
            rows.insert(0, (legacy.channel, legacy.guild, legacy.channel, legacy.guild, ""))
        elif saved[1] != legacy.guild:
            raise discord.ReadError("旧設定と登録一覧のサーバーIDが一致しません。")
    default = legacy.channel if legacy else (rows[0][0] if rows else None)
    return describe(rows, default)


def select_from(rows, channel):
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


def select(channel):
    return select_from(entries(), channel)


def set_default(channel):
    with writable_registry() as db:
        target = select_from(describe(channel_rows(db), None), channel)
        db.execute("UPDATE preferences SET default_id=?", (target.channel,))
        return {"default": target.link}


def remove(channel):
    with writable_registry() as db:
        target = select_from(describe(channel_rows(db), None), channel)
        db.execute("DELETE FROM channels WHERE id=?", (target.channel,))
        db.execute("UPDATE preferences SET default_id=? WHERE default_id=?", (first_id(db), target.channel))
        return {"removed": target.link}
