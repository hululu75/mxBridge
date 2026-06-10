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

from bridge.crypto import decrypt_config
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
    master = os.environ.get("MXBRIDGE_MASTER_KEY") or getpass.getpass("Master password: ")
    config = decrypt_config(config, master)
    section = config[args.section]

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
