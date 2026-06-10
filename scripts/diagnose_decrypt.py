#!/usr/bin/env python3
"""Diagnose why encrypted events in a room cannot be decrypted.

Run this ON THE BRIDGE MACHINE (it needs config.yaml with the recovery_key).
For each undecrypted megolm event it answers, server-side, exactly one of:

  1. KEY NOT IN BACKUP      — no client of this account ever backed up the
                              session key. Fix: export E2E keys from Element
                              (Settings > Security > Export E2E room keys) and
                              set key_import_file/key_import_passphrase.
  2. BACKUP DECRYPT FAILED  — session exists in backup but was encrypted with
                              a different/older backup key version. Fix: reset
                              key backup from a client that can read the room.
  3. INDEX TOO HIGH         — backup has the key, but its ratchet starts AFTER
                              this message (first_known_index > message index).
                              The message predates what any backed-up client
                              could decrypt. Usually unrecoverable.
  4. KEY OK IN BACKUP       — the key should decrypt this message. If the
                              bridge still fails, the problem is local (crypto
                              store / device identity), not the backup.

Usage:
  MXBRIDGE_MASTER_KEY=... python scripts/diagnose_decrypt.py \
      --config config.yaml --room '!roomid:server' [--limit 50]
  ... --room '!roomid:server' --event '$eventid'
  ... --section target   (default: source)
"""
from __future__ import annotations

import argparse
import asyncio
import base64
import getpass
import json
import os
import struct
import sys
from typing import Optional
from urllib.parse import quote

sys.path.insert(0, os.path.join(os.path.dirname(__file__), ".."))

import yaml

from bridge.crypto import decrypt, is_encrypted
from bridge.key_backup import derive_backup_key, _decrypt_backup_session


def _b64decode(s: str) -> bytes:
    return base64.b64decode(s + "=" * (-len(s) % 4))


def megolm_message_index(ciphertext_b64: str) -> Optional[int]:
    """Extract the ratchet message index from a megolm ciphertext packet.

    Packet layout: 0x03 version byte, then protobuf-style TLVs
    (0x08 = varint message-index, 0x12 = length-prefixed ciphertext),
    followed by an 8-byte MAC and 64-byte signature trailer.
    """
    try:
        raw = _b64decode(ciphertext_b64)
    except Exception:
        return None
    if len(raw) < 80 or raw[0] != 0x03:
        return None
    i = 1
    end = len(raw) - 72
    while i < end:
        tag = raw[i]
        i += 1
        if tag == 0x08:
            val = 0
            shift = 0
            while i < end:
                b = raw[i]
                i += 1
                val |= (b & 0x7F) << shift
                if not (b & 0x80):
                    return val
                shift += 7
            return None
        elif tag == 0x12:
            ln = 0
            shift = 0
            while i < end:
                b = raw[i]
                i += 1
                ln |= (b & 0x7F) << shift
                if not (b & 0x80):
                    break
                shift += 7
            i += ln
        else:
            return None
    return None


def export_first_known_index(session_key_b64: str) -> Optional[int]:
    """First ratchet index of a SESSION_EXPORT-format megolm key."""
    try:
        raw = _b64decode(session_key_b64)
    except Exception:
        return None
    if len(raw) < 5 or raw[0] != 0x01:
        return None
    return struct.unpack(">I", raw[1:5])[0]


def load_local_identity_keys(section: dict) -> Optional[dict]:
    """Read the bridge's olm account identity keys straight from the nio store."""
    import glob
    import sqlite3

    import olm

    store_path = section.get("store_path", "")
    device_id = section.get("device_id", "")
    if not store_path or not os.path.isdir(store_path):
        return None
    for db_path in glob.glob(os.path.join(store_path, "*.db")) + \
            glob.glob(os.path.join(store_path, "*.DB")):
        try:
            db = sqlite3.connect(db_path)
            row = db.execute(
                "SELECT account FROM accounts WHERE device_id = ?", (device_id,)
            ).fetchone()
            db.close()
        except sqlite3.Error:
            continue
        if row:
            pickle_b = row[0] if isinstance(row[0], bytes) else row[0].encode()
            return dict(olm.Account.from_pickle(pickle_b, "DEFAULT_KEY").identity_keys)
    return None


async def check_identity(http, hs: str, headers: dict, section: dict) -> bool:
    """Compare local olm identity keys with what the server advertises.

    Returns False on a mismatch — the fatal condition where senders encrypt
    room keys to a key the bridge does not own.
    """
    user_id = section["user_id"]
    device_id = section.get("device_id", "")
    local = load_local_identity_keys(section)
    if not local:
        print(f"identity check: cannot read local olm account for device {device_id} "
              f"(store_path={section.get('store_path', '')!r}) — skipped")
        return True
    async with http.post(
        f"{hs}/_matrix/client/v3/keys/query", headers=headers,
        json={"device_keys": {user_id: [device_id]}},
    ) as r:
        if r.status != 200:
            print(f"identity check: keys/query HTTP {r.status} — skipped")
            return True
        data = await r.json()
    server_keys = (data.get("device_keys", {}).get(user_id, {})
                   .get(device_id, {}).get("keys", {}))
    server_curve = server_keys.get(f"curve25519:{device_id}", "")
    server_ed = server_keys.get(f"ed25519:{device_id}", "")
    print(f"identity check for device {device_id}:")
    print(f"  local  curve25519={local.get('curve25519')}  ed25519={local.get('ed25519')}")
    print(f"  server curve25519={server_curve or '(none)'}  ed25519={server_ed or '(none)'}")
    if not server_curve:
        print("  -> server has NO keys for this device (bridge keys_upload may have failed)")
        return False
    if server_curve != local.get("curve25519") or server_ed != local.get("ed25519"):
        print("  -> FATAL MISMATCH: the server advertises keys this bridge does not own.")
        print("     Senders encrypt room keys to the server's key; the bridge can never")
        print("     decrypt them ('Olm event doesn't contain ciphertext for our key').")
        print("     Fix: sign out this session in Element, delete the local store/,")
        print("     clear device_id/access_token in config, restart for a fresh device.")
        return False
    print("  -> identity keys consistent")
    return True


async def fetch_events(http, hs: str, headers: dict, room_id: str,
                       event_id: Optional[str], limit: int) -> list[dict]:
    enc_room = quote(room_id, safe="")
    if event_id:
        url = f"{hs}/_matrix/client/v3/rooms/{enc_room}/event/{quote(event_id, safe='')}"
        async with http.get(url, headers=headers) as r:
            if r.status != 200:
                print(f"ERROR: cannot fetch event {event_id}: HTTP {r.status} {await r.text()}")
                return []
            return [await r.json()]
    url = f"{hs}/_matrix/client/v3/rooms/{enc_room}/messages?dir=b&limit={limit}"
    async with http.get(url, headers=headers) as r:
        if r.status != 200:
            print(f"ERROR: cannot fetch messages: HTTP {r.status} {await r.text()}")
            return []
        data = await r.json()
    return data.get("chunk", [])


async def main() -> None:
    ap = argparse.ArgumentParser(description=__doc__,
                                 formatter_class=argparse.RawDescriptionHelpFormatter)
    ap.add_argument("--config", default=os.environ.get("MXBRIDGE_CONFIG", "config.yaml"))
    ap.add_argument("--section", default="source", choices=["source", "target"])
    ap.add_argument("--room", required=True)
    ap.add_argument("--event", default="")
    ap.add_argument("--limit", type=int, default=30)
    args = ap.parse_args()

    with open(args.config) as f:
        config = yaml.safe_load(f)
    section = config[args.section]
    # Decrypt only the fields of the chosen section; no password prompt if none
    # of them are encrypted.
    enc_fields = [f for f in ("access_token", "recovery_key")
                  if is_encrypted(section.get(f, ""))]
    if enc_fields:
        master = os.environ.get("MXBRIDGE_MASTER_KEY") or getpass.getpass("Master password: ")
        for field in enc_fields:
            plain = decrypt(section[field], master)
            if plain is None:
                print(f"ERROR: cannot decrypt {args.section}.{field} — wrong master password?")
                return
            section[field] = plain

    hs = section["homeserver"].rstrip("/")
    token = section["access_token"]
    user_id = section["user_id"]
    recovery_key = section.get("recovery_key", "")
    headers = {"Authorization": f"Bearer {token}"}

    import aiohttp
    async with aiohttp.ClientSession() as http:
        async with http.get(f"{hs}/_matrix/client/v3/account/whoami", headers=headers) as r:
            if r.status != 200:
                print(f"ERROR: access_token invalid (HTTP {r.status}). "
                      f"Token may have expired — copy a fresh one from the running bridge config.")
                return
            who = await r.json()
            print(f"Authenticated as {who.get('user_id')} device={who.get('device_id')}")

        identity_ok = await check_identity(http, hs, headers, section)
        if not identity_ok:
            print("\nIdentity is broken — per-event backup checks below are secondary.\n")

        backup_priv = None
        version = ""
        if recovery_key:
            try:
                backup_priv, version = await derive_backup_key(hs, token, user_id, recovery_key)
                print(f"Backup key derived OK (backup version={version})")
            except Exception as e:
                print(f"WARNING: cannot derive backup key: {e}")
        else:
            print(f"WARNING: no recovery_key in {args.section} section — backup checks skipped")

        events = await fetch_events(http, hs, headers, args.room, args.event or None, args.limit)
        megolm = [e for e in events if e.get("type") == "m.room.encrypted"
                  and e.get("content", {}).get("algorithm") == "m.megolm.v1.aes-sha2"]
        print(f"\n{len(megolm)} megolm event(s) to check in {args.room}\n" + "=" * 70)

        verdicts: dict[str, int] = {}
        checked_sessions: dict[str, tuple] = {}
        for ev in megolm:
            content = ev.get("content", {})
            session_id = content.get("session_id", "")
            msg_index = megolm_message_index(content.get("ciphertext", ""))
            print(f"\nevent {ev.get('event_id')}  sender={ev.get('sender')}")
            print(f"  session_id={session_id}  message_index={msg_index}")

            if not backup_priv:
                continue

            if session_id in checked_sessions:
                status, first_index = checked_sessions[session_id]
            else:
                enc_room = quote(args.room, safe="")
                enc_sess = quote(session_id, safe="")
                url = (f"{hs}/_matrix/client/v3/room_keys/keys/{enc_room}/{enc_sess}"
                       f"?version={version}")
                async with http.get(url, headers=headers) as r:
                    if r.status == 404:
                        status, first_index = "missing", None
                    elif r.status != 200:
                        status, first_index = f"http {r.status}", None
                    else:
                        sdata = (await r.json()).get("session_data", {})
                        dec = _decrypt_backup_session(backup_priv, sdata)
                        if dec is None:
                            status, first_index = "undecryptable", None
                        else:
                            status = "ok"
                            first_index = export_first_known_index(dec.get("session_key", ""))
                checked_sessions[session_id] = (status, first_index)

            if status == "missing":
                verdict = "KEY NOT IN BACKUP"
            elif status == "undecryptable":
                verdict = "BACKUP DECRYPT FAILED (wrong/old backup key version)"
            elif status.startswith("http"):
                verdict = f"BACKUP QUERY FAILED ({status})"
            elif first_index is not None and msg_index is not None and first_index > msg_index:
                verdict = f"INDEX TOO HIGH (backup starts at {first_index}, message needs {msg_index})"
            else:
                verdict = f"KEY OK IN BACKUP (first_known_index={first_index})"
            verdicts[verdict.split(" (")[0]] = verdicts.get(verdict.split(" (")[0], 0) + 1
            print(f"  -> {verdict}")

        print("\n" + "=" * 70 + "\nSummary:")
        for v, n in sorted(verdicts.items(), key=lambda x: -x[1]):
            print(f"  {n:4d}  {v}")
        if verdicts.get("KEY NOT IN BACKUP"):
            print("\nFix for 'KEY NOT IN BACKUP': in Element (same account) open Settings >"
                  "\n  Security & Privacy > Export E2E room keys, copy the file to the bridge"
                  "\n  machine and set key_import_file / key_import_passphrase in config.yaml.")
        if verdicts.get("KEY OK IN BACKUP"):
            print("\n'KEY OK IN BACKUP' but bridge still fails => problem is in the bridge's"
                  "\n  local crypto store. Check bridge logs for 'IDENTITY KEY MISMATCH' or"
                  "\n  import errors; deleting the store/ directory forces a clean re-setup.")


if __name__ == "__main__":
    asyncio.run(main())
