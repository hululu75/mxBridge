from __future__ import annotations

import logging
import os
import re
import stat
import tempfile
from datetime import datetime, timezone
from typing import Optional

import peewee

from bridge.crypto import DB_KEY_SALT_SIZE, derive_db_key
from bridge.models import BridgeMessage, CallAction, MessageDirection

logger = logging.getLogger(__name__)

_DATE_RE = re.compile(r"^\d{4}-\d{2}-\d{2}([ T]\d{2}:\d{2}:\d{2})?$")
ROOMS_LIMIT = 500
SENDERS_LIMIT = 500
SQLITE_HEADER = b"SQLite format 3\x00"

db = peewee.DatabaseProxy()

MESSAGE_COLUMNS = [
    "id", "timestamp", "direction", "source_room_id", "source_room_name",
    "sender", "sender_displayname", "text", "msgtype", "event_id",
    "target_room_id", "media_url", "media_filename", "media_mimetype",
    "media_size", "call_type", "call_action", "call_duration", "from_self",
    "media_local_path", "edit_of_event_id",
]


def _utcnow():
    return datetime.now(timezone.utc)


def _sanitize_fts_query(query: str) -> str:
    sanitized = re.sub(r'[*"(){}[\]|^:&\\]', ' ', query)
    tokens = sanitized.split()
    return ' '.join(f'"{t}"' for t in tokens if t)


def _normalize_date_to(date_to: str) -> str:
    if len(date_to) <= 10:
        return date_to + " 23:59:59"
    return date_to


def _date_to_ms(date_str: str) -> int:
    if len(date_str) <= 10:
        date_str += " 00:00:00"
    dt = datetime.strptime(date_str, "%Y-%m-%d %H:%M:%S").replace(tzinfo=timezone.utc)
    return int(dt.timestamp() * 1000)


class Message(peewee.Model):
    timestamp = peewee.DateTimeField(default=_utcnow)
    direction = peewee.CharField()
    source_room_id = peewee.CharField()
    source_room_name = peewee.CharField(default="")
    sender = peewee.CharField()
    sender_displayname = peewee.CharField(default="")
    text = peewee.TextField(default="")
    msgtype = peewee.CharField(default="m.text")
    event_id = peewee.CharField(default="", unique=True)
    target_room_id = peewee.CharField(default="")
    media_url = peewee.CharField(default="")
    media_filename = peewee.CharField(default="")
    media_mimetype = peewee.CharField(default="")
    media_size = peewee.IntegerField(default=0)
    call_type = peewee.CharField(default="")
    call_action = peewee.CharField(default="")
    call_duration = peewee.IntegerField(default=0)
    from_self = peewee.BooleanField(default=False)
    media_local_path = peewee.CharField(default="")
    edit_of_event_id = peewee.CharField(default="", index=True)

    class Meta:
        database = db
        table_name = "messages"
        indexes = (
            (("sender",), False),
            (("timestamp",), False),
        )


class BridgeConfig(peewee.Model):
    key = peewee.CharField(unique=True)
    value = peewee.TextField()

    class Meta:
        database = db
        table_name = "bridge_config"


class UserAlias(peewee.Model):
    sender_id = peewee.CharField(primary_key=True)
    displayname = peewee.CharField(default="")

    class Meta:
        database = db
        table_name = "user_aliases"


class RoomAlias(peewee.Model):
    room_id = peewee.CharField(primary_key=True)
    room_name = peewee.CharField(default="")

    class Meta:
        database = db
        table_name = "room_aliases"


class ProcessedEvent(peewee.Model):
    event_id = peewee.CharField(primary_key=True)

    class Meta:
        database = db
        table_name = "state_processed_events"


class EventRoomMap(peewee.Model):
    event_id = peewee.CharField(primary_key=True)
    room_id = peewee.CharField()

    class Meta:
        database = db
        table_name = "state_event_room_map"


class SourceTargetMap(peewee.Model):
    source_event_id = peewee.CharField(primary_key=True)
    target_event_id = peewee.CharField()

    class Meta:
        database = db
        table_name = "state_source_target_map"


class FailedDecryption(peewee.Model):
    session_id = peewee.CharField()
    room_id = peewee.CharField()
    event_id = peewee.CharField()

    class Meta:
        database = db
        table_name = "state_failed_decryptions"
        primary_key = peewee.CompositeKey("session_id", "event_id")


def _validate_date(value: str) -> bool:
    return bool(_DATE_RE.match(value))


def _is_plain_sqlite(path: str) -> bool:
    if not os.path.exists(path) or os.path.getsize(path) < 16:
        return False
    try:
        with open(path, "rb") as f:
            header = f.read(16)
        return header == SQLITE_HEADER
    except OSError:
        return False


class _EncryptedSqliteDatabase(peewee.SqliteDatabase):
    def __init__(self, path: str, db_key_hex: str, **kwargs):
        self._cipher_key = db_key_hex
        super().__init__(path, **kwargs)

    @staticmethod
    def _get_sqlcipher():
        try:
            from sqlcipher3 import dbapi2 as _sqlite3
            return _sqlite3
        except ImportError:
            from pysqlcipher3 import dbapi2 as _sqlite3
            return _sqlite3

    def _connect(self):
        _sqlite3 = self._get_sqlcipher()
        conn = _sqlite3.connect(self.database, timeout=self._timeout,
                                isolation_level=None, **self.connect_params)
        conn.execute(f"PRAGMA key = \"x'{self._cipher_key}'\"")
        conn.execute("PRAGMA cipher_page_size = 4096")
        conn.execute("PRAGMA cipher_hmac_algorithm = HMAC_SHA256")
        try:
            self._add_conn_hooks(conn)
        except Exception:
            conn.close()
            raise
        return conn


def _create_encrypted_db(path: str, db_key: str) -> peewee.SqliteDatabase:
    return _EncryptedSqliteDatabase(
        path,
        db_key_hex=db_key,
        pragmas={
            "journal_mode": "wal",
            "busy_timeout": 5000,
        },
    )


def _create_plain_db(path: str) -> peewee.SqliteDatabase:
    return peewee.SqliteDatabase(path, pragmas={
        "journal_mode": "wal",
        "busy_timeout": 5000,
    })


def _load_or_create_salt(salt_path: str) -> bytes:
    if os.path.exists(salt_path):
        with open(salt_path, "rb") as f:
            salt = f.read(DB_KEY_SALT_SIZE)
        if len(salt) == DB_KEY_SALT_SIZE:
            return salt
        logger.warning("Salt file %s is corrupted, regenerating", salt_path)
    else:
        logger.info("Generating new database encryption salt: %s", salt_path)
    salt = os.urandom(DB_KEY_SALT_SIZE)
    salt_dir = os.path.dirname(salt_path) or "."
    fd, tmp_path = tempfile.mkstemp(dir=salt_dir, prefix=".salt_tmp_")
    try:
        os.write(fd, salt)
        os.close(fd)
        os.chmod(tmp_path, stat.S_IRUSR | stat.S_IWUSR)
        os.replace(tmp_path, salt_path)
    except Exception:
        try:
            os.close(fd)
        except OSError:
            pass
        try:
            os.unlink(tmp_path)
        except OSError:
            pass
        raise
    return salt


ALL_MODELS = [
    Message, BridgeConfig, UserAlias, RoomAlias,
    ProcessedEvent, EventRoomMap, SourceTargetMap, FailedDecryption,
]


class MessageStore:
    def __init__(self, path: str = "messages.db", media_dir: str = "",
                 db_password: str = ""):
        self._path = path
        self._media_dir = media_dir
        self._fts_available = False
        self._encrypted = bool(db_password)

        if db_password:
            try:
                _EncryptedSqliteDatabase._get_sqlcipher()
            except ImportError:
                logger.error(
                    "pysqlcipher3 is required for database encryption. "
                    "Install with: pip install pysqlcipher3 "
                    "(requires libsqlcipher-dev on Debian/Ubuntu, "
                    "sqlcipher-libs on Alpine)",
                )
                raise

            salt_path = path + ".salt"
            salt = _load_or_create_salt(salt_path)
            db_key = derive_db_key(db_password, salt)

            if os.path.exists(path) and _is_plain_sqlite(path):
                self._real_db = self._migrate_to_encrypted(path, db_key)
            else:
                self._real_db = _create_encrypted_db(path, db_key)
        else:
            self._real_db = _create_plain_db(path)

        db.initialize(self._real_db)
        db.connect(reuse_if_open=True)
        db.create_tables(ALL_MODELS, safe=True)
        os.chmod(path, stat.S_IRUSR | stat.S_IWUSR)
        self._migrate()
        self._ensure_indexes()
        self._init_fts()
        self._init_fts_triggers()

    def _migrate_to_encrypted(self, path: str, db_key: str) -> peewee.SqliteDatabase:
        logger.info("Detected plaintext database, migrating to encrypted...")

        backup_path = path + ".plaintext.bak"
        for ext in ("", "-wal", "-shm"):
            src = path + ext
            dst = backup_path + ext
            if os.path.exists(src):
                os.replace(src, dst)
                try:
                    os.chmod(dst, stat.S_IRUSR | stat.S_IWUSR)
                except OSError:
                    pass

        backup_db = peewee.SqliteDatabase(backup_path, pragmas={
            "journal_mode": "wal", "busy_timeout": 5000,
        })
        backup_db.connect()

        table_columns = {}
        row_counts = {}
        for model in ALL_MODELS:
            table = model._meta.table_name
            try:
                cursor = backup_db.execute_sql(
                    f"SELECT * FROM [{table}] LIMIT 0"
                )
                columns = (
                    [desc[0] for desc in cursor.description]
                    if cursor.description else []
                )
                if columns:
                    table_columns[table] = columns
                count = backup_db.execute_sql(
                    f"SELECT COUNT(*) FROM [{table}]"
                ).fetchone()[0]
                row_counts[table] = count
            except peewee.OperationalError:
                table_columns[table] = []
                row_counts[table] = 0

        enc_db = None
        try:
            enc_db = _create_encrypted_db(path, db_key)
            enc_db.connect()
            db.initialize(enc_db)
            db.create_tables(ALL_MODELS, safe=True)

            for table, columns in table_columns.items():
                if not columns or row_counts.get(table, 0) == 0:
                    continue
                placeholders = ", ".join(["?"] * len(columns))
                cols = ", ".join(f"[{c}]" for c in columns)
                sql = f"INSERT INTO [{table}] ({cols}) VALUES ({placeholders})"

                cursor = backup_db.execute_sql(f"SELECT * FROM [{table}]")
                batch_num = 0
                while True:
                    batch = cursor.fetchmany(1000)
                    if not batch:
                        break
                    with enc_db.atomic():
                        for row in batch:
                            enc_db.execute_sql(sql, row)
                    batch_num += 1
                    if batch_num % 10 == 0:
                        logger.info(
                            "Migration: %s - %d rows copied",
                            table, batch_num * 1000,
                        )

            backup_db.close()

            for table, expected in row_counts.items():
                if expected == 0:
                    continue
                count = enc_db.execute_sql(
                    f"SELECT COUNT(*) FROM [{table}]"
                ).fetchone()[0]
                if count != expected:
                    raise RuntimeError(
                        f"Migration verification failed for {table}: "
                        f"expected {expected} rows, got {count}"
                    )

            os.chmod(path, stat.S_IRUSR | stat.S_IWUSR)
            logger.info(
                "Database migration completed. Backup kept at %s "
                "- delete manually after verifying.",
                backup_path,
            )
            return enc_db
        except Exception:
            if enc_db:
                try:
                    enc_db.close()
                except Exception:
                    pass
            try:
                backup_db.close()
            except Exception:
                pass
            if os.path.exists(path):
                for ext in ("", "-wal", "-shm"):
                    p = path + ext
                    if os.path.exists(p):
                        os.remove(p)
            for ext in ("", "-wal", "-shm"):
                src = backup_path + ext
                dst = path + ext
                if os.path.exists(src):
                    os.replace(src, dst)
            logger.error("Database migration failed, reverted to plaintext")
            raise

    def _migrate(self) -> None:
        try:
            cur = db.execute_sql("PRAGMA table_info(messages)")
            existing = {row[1] for row in cur.fetchall()}
            if "from_self" not in existing:
                db.execute_sql("ALTER TABLE messages ADD COLUMN from_self INTEGER DEFAULT 0")
                logger.info("Migrated messages table: added from_self column")
            if "media_local_path" not in existing:
                db.execute_sql("ALTER TABLE messages ADD COLUMN media_local_path TEXT DEFAULT ''")
                logger.info("Migrated messages table: added media_local_path column")
            if "edit_of_event_id" not in existing:
                db.execute_sql("ALTER TABLE messages ADD COLUMN edit_of_event_id TEXT DEFAULT ''")
                db.execute_sql("CREATE INDEX IF NOT EXISTS messages_edit_of_event_id ON messages (edit_of_event_id)")
                logger.info("Migrated messages table: added edit_of_event_id column")
        except Exception:
            logger.warning("Failed to check/migrate messages table schema", exc_info=True)

        try:
            key = "migrated_aliases_v1"
            row = BridgeConfig.get_or_none(BridgeConfig.key == key)
            if not row:
                db.execute_sql(
                    "INSERT OR IGNORE INTO user_aliases (sender_id, displayname) "
                    "SELECT sender, sender_displayname FROM messages "
                    "WHERE sender_displayname != '' AND sender_displayname != sender "
                    "GROUP BY sender HAVING id = MAX(id)"
                )
                db.execute_sql(
                    "INSERT OR IGNORE INTO room_aliases (room_id, room_name) "
                    "SELECT source_room_id, source_room_name FROM messages "
                    "WHERE source_room_name != '' AND source_room_name != source_room_id "
                    "GROUP BY source_room_id HAVING id = MAX(id)"
                )
                logger.info("Migrated: populated alias tables from messages")
                BridgeConfig.create(key=key, value="1")
        except Exception:
            logger.warning("Failed to migrate alias tables", exc_info=True)

    def _ensure_indexes(self) -> None:
        idxs = [
            ("idx_messages_room_ts",
             "ON messages (source_room_id, timestamp DESC)"),
            ("idx_messages_room_sender",
             "ON messages (source_room_id, sender)"),
        ]
        for name, definition in idxs:
            try:
                db.execute_sql(f"CREATE INDEX IF NOT EXISTS {name} {definition}")
            except Exception:
                logger.warning("Failed to create index %s", name)

    def _init_fts(self) -> None:
        try:
            cursor = db.execute_sql(
                "SELECT name FROM sqlite_master WHERE type='table' AND name='messages_fts'"
            )
            if cursor.fetchone() is None:
                db.execute_sql(
                    "CREATE VIRTUAL TABLE messages_fts USING fts5("
                    "text, content='messages', content_rowid=id)"
                )
                db.execute_sql(
                    "INSERT INTO messages_fts(messages_fts) VALUES('rebuild')"
                )
            self._fts_available = True
        except Exception:
            logger.warning("FTS5 not available, falling back to LIKE search")
            self._fts_available = False

    def _init_fts_triggers(self) -> None:
        if not self._fts_available:
            return
        try:
            db.execute_sql(
                "CREATE TRIGGER IF NOT EXISTS messages_fts_ai AFTER INSERT ON messages "
                "BEGIN INSERT INTO messages_fts(rowid, text) VALUES (NEW.id, NEW.text); END"
            )
            db.execute_sql(
                "CREATE TRIGGER IF NOT EXISTS messages_fts_ad AFTER DELETE ON messages "
                "BEGIN INSERT INTO messages_fts(messages_fts, rowid, text) "
                "VALUES('delete', OLD.id, OLD.text); END"
            )
            db.execute_sql(
                "CREATE TRIGGER IF NOT EXISTS messages_fts_au AFTER UPDATE ON messages "
                "BEGIN "
                "INSERT INTO messages_fts(messages_fts, rowid, text) VALUES('delete', OLD.id, OLD.text); "
                "INSERT INTO messages_fts(rowid, text) VALUES (NEW.id, NEW.text); "
                "END"
            )
        except Exception:
            logger.warning("Failed to create FTS triggers")

    def clear_all(self) -> None:
        try:
            with db.atomic():
                db.execute_sql("DELETE FROM messages")
                if self._fts_available:
                    db.execute_sql("INSERT INTO messages_fts(messages_fts) VALUES('rebuild')")
                db.execute_sql("DELETE FROM bridge_config WHERE key != 'web_secret'")
                db.execute_sql("DELETE FROM user_aliases")
                db.execute_sql("DELETE FROM room_aliases")
            logger.info("All message data cleared")
        except Exception:
            logger.error("Failed to clear data", exc_info=True)

    def rebuild_fts(self) -> None:
        if not self._fts_available:
            return
        try:
            db.execute_sql("INSERT INTO messages_fts(messages_fts) VALUES('rebuild')")
            logger.info("FTS index rebuilt")
        except Exception:
            logger.error("Failed to rebuild FTS", exc_info=True)

    def close(self) -> None:
        try:
            if not self._real_db.is_closed():
                self._real_db.close()
        except Exception:
            pass

    def get_or_create_secret(self) -> str:
        try:
            row = BridgeConfig.get(BridgeConfig.key == "web_secret")
            return row.value
        except BridgeConfig.DoesNotExist:
            secret = os.urandom(32).hex()
            BridgeConfig.create(key="web_secret", value=secret)
            return secret

    def event_id_exists(self, event_id: str) -> bool:
        try:
            return Message.select(Message.id).where(Message.event_id == event_id).exists()
        except Exception:
            return False

    def save_message(self, msg: BridgeMessage, media_dir: str = "") -> None:
        if not msg.event_id:
            return
        media_local_path = ""
        if media_dir and msg.media_data:
            media_local_path = self._save_media_file(msg, media_dir)
        try:
            with db.atomic():
                row = Message.create(
                    timestamp=msg.timestamp,
                    direction=msg.direction.value,
                    source_room_id=msg.source_room_id,
                    source_room_name=msg.source_room_name,
                    sender=msg.sender,
                    sender_displayname=msg.sender_displayname,
                    text=msg.text,
                    msgtype=msg.msgtype.value,
                    event_id=msg.event_id,
                    target_room_id=msg.target_room_id or "",
                    media_url=msg.media_url or "",
                    media_filename=msg.media_filename or "",
                    media_mimetype=msg.media_mimetype or "",
                    media_size=msg.media_size or 0,
                    call_type=msg.call_type or "",
                    call_action=msg.call_action.value if msg.call_action else "",
                    call_duration=msg.call_duration or 0,
                    from_self=msg.from_self,
                    media_local_path=media_local_path,
                    edit_of_event_id=msg.edit_of_event_id or "",
                )
        except peewee.IntegrityError:
            if media_local_path:
                full_path = os.path.join(media_dir, media_local_path)
                try:
                    os.unlink(full_path)
                except OSError:
                    pass
        except Exception:
            logger.error("Failed to save message %s", msg.event_id, exc_info=True)

    def update_message_text(self, event_id: str, new_text: str) -> bool:
        if not event_id or not new_text:
            return False
        try:
            with db.atomic():
                rows = Message.update(text=new_text).where(Message.event_id == event_id).execute()
                return rows > 0
        except Exception:
            logger.error("Failed to update message %s", event_id, exc_info=True)
            return False

    def reconcile_edits(self) -> int:
        updated = 0
        try:
            edits = list(Message.select(
                Message.event_id, Message.edit_of_event_id, Message.text, Message.timestamp,
            ).where(
                (Message.direction == "edit") & (Message.edit_of_event_id != ""),
            ).order_by(Message.timestamp.asc()))
            latest_by_original: dict[str, tuple[str, str]] = {}
            edit_event_ids: set[str] = set()
            for e in edits:
                latest_by_original[e.edit_of_event_id] = (e.event_id, e.text)
                edit_event_ids.add(e.event_id)
            resolved: dict[str, tuple[str, str]] = {}
            for orig_id, (edit_eid, text) in latest_by_original.items():
                current_id = orig_id
                current_text = text
                visited = set()
                while current_id in latest_by_original:
                    if current_id in visited:
                        break
                    visited.add(current_id)
                    next_eid, current_text = latest_by_original[current_id]
                    current_id = next_eid
                resolved[orig_id] = (edit_eid, current_text)
            for orig_id, (edit_eid, text) in resolved.items():
                orig_exists = Message.select(Message.id).where(
                    Message.event_id == orig_id,
                ).exists()
                if orig_exists:
                    Message.update(text=text).where(
                        Message.event_id == orig_id,
                    ).execute()
                    Message.delete().where(
                        Message.event_id == edit_eid,
                    ).execute()
                    updated += 1
                else:
                    Message.update(direction="forward", edit_of_event_id="").where(
                        Message.event_id == edit_eid,
                    ).execute()
                    updated += 1
        except Exception:
            logger.error("Failed to reconcile edits", exc_info=True)
        return updated

    def upsert_user_alias(self, sender: str, displayname: str) -> None:
        if not sender or not displayname or displayname == sender:
            return
        try:
            with db.atomic():
                UserAlias.insert(sender_id=sender, displayname=displayname).on_conflict(
                    conflict_target=[UserAlias.sender_id],
                    update={UserAlias.displayname: displayname},
                ).execute()
        except Exception:
            logger.error("Failed to upsert user alias for %s", sender, exc_info=True)

    def upsert_room_alias(self, room_id: str, room_name: str) -> None:
        if not room_id or not room_name or room_name == room_id:
            return
        try:
            with db.atomic():
                RoomAlias.insert(room_id=room_id, room_name=room_name).on_conflict(
                    conflict_target=[RoomAlias.room_id],
                    update={RoomAlias.room_name: room_name},
                ).execute()
        except Exception:
            logger.error("Failed to upsert room alias for %s", room_id, exc_info=True)

    def delete_message(self, event_id: str) -> bool:
        if not event_id:
            return False
        try:
            with db.atomic():
                msg = Message.get(Message.event_id == event_id)
                if msg.media_local_path and self._media_dir:
                    full_path = os.path.join(self._media_dir, msg.media_local_path)
                    try:
                        os.unlink(full_path)
                    except OSError:
                        pass
                msg.delete_instance()
                return True
        except Message.DoesNotExist:
            return False
        except Exception:
            logger.error("Failed to delete message %s", event_id, exc_info=True)
            return False

    def _save_media_file(self, msg: BridgeMessage, media_dir: str) -> str:
        data = msg.media_data
        if not data:
            return ""
        ts = msg.timestamp
        if isinstance(ts, (int, float)):
            ts = datetime.fromtimestamp(ts / 1000, tz=timezone.utc)
        subdir = ts.strftime("%Y-%m")
        safe_event = re.sub(r'[^a-zA-Z0-9_\-]', '_', msg.event_id)
        safe_name = re.sub(r'[^a-zA-Z0-9_.\-]', '_', msg.media_filename or "file")
        filename = f"{safe_event}_{safe_name}"
        dest_dir = os.path.join(media_dir, subdir)
        local_path = os.path.join(subdir, filename)
        full_path = os.path.join(media_dir, local_path)
        try:
            os.makedirs(dest_dir, exist_ok=True)
            fd, tmp_path = tempfile.mkstemp(dir=dest_dir, prefix=".tmp_")
            try:
                os.write(fd, data)
                os.close(fd)
                os.chmod(tmp_path, 0o644)
                os.replace(tmp_path, full_path)
            except Exception:
                try:
                    os.close(fd)
                except OSError:
                    pass
                try:
                    os.unlink(tmp_path)
                except OSError:
                    pass
                raise
            return local_path
        except Exception:
            logger.error("Failed to save media file for %s", msg.event_id, exc_info=True)
            return ""

    def get_media_path(self, event_id: str) -> Optional[str]:
        try:
            msg = Message.get(Message.event_id == event_id)
        except Message.DoesNotExist:
            return None
        if not msg.media_local_path:
            return None
        return msg.media_local_path

    def search_messages(
        self,
        query: str = "",
        room_id: str = "",
        sender: str = "",
        date_from: Optional[str] = None,
        date_to: Optional[str] = None,
        page: int = 1,
        limit: int = 50,
    ) -> dict:
        if self._fts_available and query:
            return self._search_fts(query, room_id, sender, date_from, date_to, page, limit)
        return self._search_like(query, room_id, sender, date_from, date_to, page, limit)

    def _search_fts(
        self,
        query: str,
        room_id: str,
        sender: str,
        date_from: Optional[str],
        date_to: Optional[str],
        page: int,
        limit: int,
    ) -> dict:
        clauses = []
        params: list = []
        safe_query = _sanitize_fts_query(query)
        if not safe_query:
            return self._search_like(query, room_id, sender, date_from, date_to, page, limit)
        clauses.append("m.id IN (SELECT rowid FROM messages_fts WHERE messages_fts MATCH ?)")
        params.append(safe_query)
        if room_id:
            clauses.append("m.source_room_id = ?")
            params.append(room_id)
        if sender:
            clauses.append("m.sender = ?")
            params.append(sender)
        if date_from and _validate_date(date_from):
            clauses.append("m.timestamp >= ?")
            params.append(_date_to_ms(date_from))
        if date_to and _validate_date(date_to):
            clauses.append("m.timestamp <= ?")
            params.append(_date_to_ms(_normalize_date_to(date_to)))

        where = " AND ".join(clauses)
        cols = ", ".join(f"m.{c}" for c in MESSAGE_COLUMNS)
        count_sql = f"SELECT COUNT(*) FROM messages m WHERE {where}"
        data_sql = (
            f"SELECT {cols} FROM messages m WHERE {where}"
            " ORDER BY m.timestamp DESC LIMIT ? OFFSET ?"
        )

        count_cur = db.execute_sql(count_sql, params)
        total = count_cur.fetchone()[0]
        offset = (page - 1) * limit
        data_cur = db.execute_sql(data_sql, params + [limit, offset])
        rows = [self._row_to_dict(row) for row in data_cur.fetchall()]
        rows = self._enrich_aliases(rows)
        return {"total": total, "page": page, "limit": limit, "results": rows}

    def _search_like(
        self,
        query: str,
        room_id: str,
        sender: str,
        date_from: Optional[str],
        date_to: Optional[str],
        page: int,
        limit: int,
    ) -> dict:
        q = Message.select()
        if query:
            q = q.where(Message.text.contains(query))
        if room_id:
            q = q.where(Message.source_room_id == room_id)
        if sender:
            q = q.where(Message.sender == sender)
        if date_from and _validate_date(date_from):
            q = q.where(Message.timestamp >= _date_to_ms(date_from))
        if date_to and _validate_date(date_to):
            q = q.where(Message.timestamp <= _date_to_ms(_normalize_date_to(date_to)))

        total = q.count()
        offset = (page - 1) * limit
        rows = q.order_by(Message.timestamp.desc()).offset(offset).limit(limit)
        results = [self._model_to_dict(m) for m in rows]
        results = self._enrich_aliases(results)
        return {"total": total, "page": page, "limit": limit, "results": results}

    def get_room_history(
        self, room_id: str, page: int = 1, limit: int = 50
    ) -> dict:
        q = Message.select().where(Message.source_room_id == room_id)
        total = q.count()
        offset = (page - 1) * limit
        rows = q.order_by(Message.timestamp.asc()).offset(offset).limit(limit)
        results = [self._model_to_dict(m) for m in rows]
        results = self._enrich_aliases(results)
        return {"total": total, "page": page, "limit": limit, "results": results}

    def get_message_context(
        self, event_id: str, before: int = 25, after: int = 25
    ) -> dict:
        try:
            msg = Message.get(Message.event_id == event_id)
        except Message.DoesNotExist:
            return {}

        before_rows = (
            Message.select()
            .where(Message.source_room_id == msg.source_room_id, Message.id < msg.id)
            .order_by(Message.id.desc())
            .limit(before)
        )
        after_rows = (
            Message.select()
            .where(Message.source_room_id == msg.source_room_id, Message.id > msg.id)
            .order_by(Message.id.asc())
            .limit(after)
        )

        target_dict = self._model_to_dict(msg)
        before_dicts = list(reversed([self._model_to_dict(m) for m in before_rows]))
        after_dicts = [self._model_to_dict(m) for m in after_rows]
        enriched = self._enrich_aliases([target_dict] + before_dicts + after_dicts)
        n_before = len(before_dicts)

        return {
            "target": enriched[0],
            "before": enriched[1:1 + n_before],
            "after": enriched[1 + n_before:],
            "room_id": msg.source_room_id,
            "room_name": msg.source_room_name,
        }

    def get_rooms(self) -> list[dict]:
        cur = db.execute_sql(
            "SELECT m.source_room_id, m.source_room_name, "
            "COALESCE(ra.room_name, m.source_room_name, m.source_room_id) as best_name, "
            "COUNT(*) as cnt, MAX(m.timestamp) as last_msg "
            "FROM messages m "
            "LEFT JOIN room_aliases ra ON ra.room_id = m.source_room_id "
            "GROUP BY m.source_room_id ORDER BY last_msg DESC LIMIT ?",
            (ROOMS_LIMIT,),
        )
        results = []
        for row in cur.fetchall():
            last_msg = row[4]
            if isinstance(last_msg, datetime):
                last_msg = last_msg.isoformat()
            elif isinstance(last_msg, str):
                last_msg = last_msg.replace(" ", "T")
            results.append({
                "room_id": row[0],
                "room_name": row[2],
                "message_count": row[3],
                "last_message": last_msg,
            })
        return results

    def get_senders(self, room_id: str = "") -> list[dict]:
        if room_id:
            cur = db.execute_sql(
                "SELECT m.sender, COALESCE(ua.displayname, m.sender_displayname, m.sender) as dn, COUNT(*) as cnt "
                "FROM messages m LEFT JOIN user_aliases ua ON ua.sender_id = m.sender "
                "WHERE m.source_room_id = ? "
                "GROUP BY m.sender ORDER BY cnt DESC LIMIT ?",
                (room_id, SENDERS_LIMIT),
            )
        else:
            cur = db.execute_sql(
                "SELECT m.sender, COALESCE(ua.displayname, m.sender_displayname, m.sender) as dn, COUNT(*) as cnt "
                "FROM messages m LEFT JOIN user_aliases ua ON ua.sender_id = m.sender "
                "GROUP BY m.sender ORDER BY cnt DESC LIMIT ?",
                (SENDERS_LIMIT,),
            )
        return [
            {"sender": row[0], "displayname": row[1], "count": row[2]}
            for row in cur.fetchall()
        ]

    def _format_timestamp(self, ts) -> Optional[str]:
        if isinstance(ts, datetime):
            return ts.isoformat()
        if isinstance(ts, (int, float)):
            return datetime.fromtimestamp(ts / 1000, tz=timezone.utc).isoformat()
        if isinstance(ts, str):
            return ts.replace(" ", "T")
        return None

    def get_stats(self) -> dict:
        total = Message.select().count()
        rooms = db.execute_sql(
            "SELECT COUNT(DISTINCT source_room_id) FROM messages"
        ).fetchone()[0]
        forward = Message.select().where(Message.direction == "forward").count()
        reply = Message.select().where(Message.direction == "reply").count()
        earliest = None
        latest = None
        if total > 0:
            first = Message.select().order_by(Message.timestamp.asc()).first()
            last = Message.select().order_by(Message.timestamp.desc()).first()
            if first:
                earliest = self._format_timestamp(first.timestamp)
            if last:
                latest = self._format_timestamp(last.timestamp)
        return {
            "total_messages": total,
            "total_rooms": rooms,
            "forward_count": forward,
            "reply_count": reply,
            "earliest_message": earliest,
            "latest_message": latest,
        }

    def _row_to_dict(self, row: tuple) -> dict:
        result = {}
        for i, col in enumerate(MESSAGE_COLUMNS):
            if i < len(row):
                val = row[i]
                if col == "timestamp":
                    if isinstance(val, datetime):
                        val = val.isoformat()
                    elif isinstance(val, (int, float)):
                        val = datetime.fromtimestamp(val / 1000, tz=timezone.utc).isoformat()
                    elif isinstance(val, str):
                        val = val.replace(" ", "T")
                result[col] = val
        return result

    def _model_to_dict(self, m: Message) -> dict:
        return {
            "id": m.id,
            "timestamp": self._format_timestamp(m.timestamp),
            "direction": m.direction,
            "source_room_id": m.source_room_id,
            "source_room_name": m.source_room_name,
            "sender": m.sender,
            "sender_displayname": m.sender_displayname,
            "text": m.text,
            "msgtype": m.msgtype,
            "event_id": m.event_id,
            "target_room_id": m.target_room_id,
            "media_url": m.media_url,
            "media_filename": m.media_filename,
            "media_mimetype": m.media_mimetype,
            "media_size": m.media_size,
            "call_type": m.call_type,
            "call_action": m.call_action,
            "call_duration": m.call_duration,
            "from_self": m.from_self,
            "media_local_path": m.media_local_path,
            "edit_of_event_id": m.edit_of_event_id,
        }

    def _enrich_aliases(self, results: list[dict]) -> list[dict]:
        if not results:
            return results
        user_map: dict[str, str] = {}
        room_map: dict[str, str] = {}
        try:
            for row in db.execute_sql("SELECT sender_id, displayname FROM user_aliases").fetchall():
                user_map[row[0]] = row[1]
        except Exception:
            pass
        try:
            for row in db.execute_sql("SELECT room_id, room_name FROM room_aliases").fetchall():
                room_map[row[0]] = row[1]
        except Exception:
            pass
        if not user_map and not room_map:
            return results
        for r in results:
            sd = r.get("sender_displayname", "")
            s = r.get("sender", "")
            if (not sd or sd == s) and s in user_map:
                r["sender_displayname"] = user_map[s]
            rn = r.get("source_room_name", "")
            ri = r.get("source_room_id", "")
            if (not rn or rn == ri) and ri in room_map:
                r["source_room_name"] = room_map[ri]
        return results
