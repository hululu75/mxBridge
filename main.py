from __future__ import annotations

import asyncio
import copy
import getpass
import logging
import logging.handlers
import os
import signal
import stat
import sys

import yaml
from nio import AsyncClient

from backends.matrix_source import MatrixSourceBackend
from backends.matrix_target import MatrixTargetBackend
from bridge.core import BridgeCore
from bridge.crypto import decrypt_config, encrypt, is_encrypted
from bridge.message_store import MessageStore
from bridge.state import StateManager

ALWAYS = 60
logging.addLevelName(ALWAYS, "ALWAYS")

logger = logging.getLogger("matrix_bridge")


def _make_formatter() -> logging.Formatter:
    return logging.Formatter(
        "%(asctime)s [%(name)s] %(levelname)s: %(message)s",
        datefmt="%Y-%m-%d %H:%M:%S",
    )


def setup_logging(config: dict) -> None:
    log_cfg = config.get("logging", {})
    level = getattr(logging, log_cfg.get("level", "INFO").upper(), logging.INFO)
    fmt = _make_formatter()
    root = logging.getLogger()
    root.setLevel(level)

    if root.handlers:
        root.handlers.clear()

    for name in list(logging.root.manager.loggerDict):
        logging.getLogger(name).setLevel(logging.NOTSET)

    log_file = log_cfg.get("file", "")
    if log_file:
        fh = logging.handlers.RotatingFileHandler(
            log_file,
            maxBytes=log_cfg.get("max_bytes", 10 * 1024 * 1024),
            backupCount=log_cfg.get("backup_count", 3),
        )
        fh.setLevel(level)
        fh.setFormatter(fmt)
        root.addHandler(fh)
    else:
        sh = logging.StreamHandler()
        sh.setLevel(level)
        sh.setFormatter(fmt)
        root.addHandler(sh)


def load_config(path: str) -> dict:
    with open(path, "r") as f:
        return yaml.safe_load(f)


async def _matrix_login(homeserver: str, user_id: str, password: str, device_id: str = "") -> tuple[str, str]:
    """Returns (access_token, device_id).  Pass device_id="" to let the server assign a new one."""
    client = AsyncClient(homeserver=homeserver, user=user_id, device_id=device_id)
    try:
        resp = await client.login(password)
        if hasattr(resp, "access_token"):
            return resp.access_token, resp.device_id
        raise RuntimeError(str(resp))
    finally:
        await client.close()


async def setup_credentials(config: dict, config_path: str, master_password: str = "") -> dict:
    """
    For backends missing access_token: login with password, encrypt the token
    with a master password, write back to config file, and return a runtime
    config with plaintext tokens.
    """
    sections_needing_setup = [
        key for key in ("source", "target")
        if config.get(key)
        and isinstance(config.get(key), dict)
        and not config[key].get("access_token")
        and config[key].get("homeserver")
    ]
    if not sections_needing_setup:
        return config

    if not master_password:
        master_password = os.environ.get("MXBIRDGE_MASTER_KEY") or getpass.getpass("Set master password for config encryption: ")
        if not master_password:
            raise ValueError("Master password is required (set MXBIRDGE_MASTER_KEY env or enter interactively)")
        confirm = master_password if os.environ.get("MXBIRDGE_MASTER_KEY") else getpass.getpass("Confirm master password: ")
        if master_password != confirm:
            raise ValueError("Master passwords do not match")

    runtime_config = copy.deepcopy(config)
    save_config = copy.deepcopy(config)

    for section_key in sections_needing_setup:
        section = config[section_key]
        homeserver = section["homeserver"]
        user_id = section["user_id"]
        device_id = section.get("device_id") or ""  # empty → server assigns a fresh device_id
        password = section.get("password", "") or getpass.getpass(f"[{section_key}] Password for {user_id}: ")

        logger.info("[%s] Logging in as %s ...", section_key, user_id)
        try:
            access_token, actual_device_id = await _matrix_login(homeserver, user_id, password, device_id)
        except Exception as e:
            raise RuntimeError(f"Login failed for {section_key} ({user_id}): {e}")
        logger.info("[%s] Login successful, device_id=%s", section_key, actual_device_id)

        key_file = input(f"[{section_key}] Path to encryption key file (Enter to skip): ").strip()
        if key_file:
            key_passphrase = getpass.getpass(f"[{section_key}] Key file passphrase: ")
            try:
                tmp_client = AsyncClient(homeserver=homeserver, user=user_id, device_id=actual_device_id)
                tmp_client.restore_login(user_id=user_id, device_id=actual_device_id, access_token=access_token)
                tmp_client.store_path = f"./store/{section_key}"
                os.makedirs(tmp_client.store_path, exist_ok=True)
                await tmp_client.import_keys(key_file, key_passphrase)
                logger.info("[%s] Encryption keys imported from %s", section_key, key_file)
                await tmp_client.close()
            except Exception as e:
                logger.warning("[%s] Failed to import keys: %s", section_key, e)
                try:
                    await tmp_client.close()
                except Exception:
                    pass

        save_config[section_key]["access_token"] = encrypt(access_token, master_password)
        save_config[section_key]["password"] = ""
        save_config[section_key]["device_id"] = actual_device_id
        runtime_config[section_key]["access_token"] = access_token
        runtime_config[section_key]["password"] = ""
        runtime_config[section_key]["device_id"] = actual_device_id

    tmp_path = config_path + ".tmp"
    with open(tmp_path, "w") as f:
        yaml.dump(save_config, f, allow_unicode=True, default_flow_style=False, sort_keys=False)
    os.replace(tmp_path, config_path)
    os.chmod(config_path, stat.S_IRUSR | stat.S_IWUSR)
    logger.info("Encrypted credentials saved to %s", config_path)

    return runtime_config


def _config_needs_key(config: dict) -> bool:
    for section_key in ("source", "target"):
        section = config.get(section_key)
        if not isinstance(section, dict):
            continue
        for field in ("access_token", "password", "key_import_passphrase"):
            value = section.get(field, "")
            if is_encrypted(value):
                return True
    web_password = config.get("bridge", {}).get("web", {}).get("password", "")
    if is_encrypted(web_password):
        return True
    return False


def _check_config_writable(config_path: str) -> None:
    if not os.path.isfile(config_path):
        return
    if not os.access(config_path, os.W_OK):
        logger.error(
            "Config file %s is not writable. "
            "The bridge needs write access to encrypt plaintext credentials.",
            config_path,
        )
        sys.exit(1)


def _has_plaintext_credentials(config: dict) -> bool:
    sensitive_fields = ("access_token", "password", "key_import_passphrase")
    for section_key in ("source", "target"):
        section = config.get(section_key)
        if not isinstance(section, dict):
            continue
        for field in sensitive_fields:
            value = section.get(field, "")
            if value and not is_encrypted(value):
                return True
    web_password = config.get("bridge", {}).get("web", {}).get("password", "")
    if web_password and not is_encrypted(web_password):
        return True
    return False


def _auto_encrypt_plaintext_fields(
    config: dict, master_key: str, config_path: str,
) -> dict:
    sensitive_fields = ("access_token", "password", "key_import_passphrase")
    changed = False
    save_config = copy.deepcopy(config)

    for section_key in ("source", "target"):
        section = save_config.get(section_key)
        if not isinstance(section, dict):
            continue
        for field in sensitive_fields:
            value = section.get(field, "")
            if value and not is_encrypted(value):
                section[field] = encrypt(value, master_key)
                changed = True

    web_section = save_config.get("bridge", {}).get("web", {})
    if isinstance(web_section, dict):
        wp = web_section.get("password", "")
        if wp and not is_encrypted(wp):
            web_section["password"] = encrypt(wp, master_key)
            changed = True

    if not changed:
        return config

    tmp_path = config_path + ".tmp"
    with open(tmp_path, "w") as f:
        yaml.dump(save_config, f, allow_unicode=True, default_flow_style=False, sort_keys=False)
    os.replace(tmp_path, config_path)
    os.chmod(config_path, stat.S_IRUSR | stat.S_IWUSR)
    logger.info("Plaintext credentials encrypted and saved to %s", config_path)

    return save_config


async def main() -> None:
    config_path = os.environ.get("MXBRIDGE_CONFIG", "config.yaml")
    if len(sys.argv) > 1:
        config_path = sys.argv[1]

    try:
        config = load_config(config_path)
    except FileNotFoundError:
        logging.basicConfig(level=logging.INFO)
        logger.error("Config file not found: %s", config_path)
        logger.error("Copy config.example.yaml to config.yaml and fill in your settings.")
        sys.exit(1)
    except yaml.YAMLError as e:
        logging.basicConfig(level=logging.INFO)
        logger.error("Invalid config: %s", e)
        sys.exit(1)

    setup_logging(config)

    master_key = os.environ.get("MXBIRDGE_MASTER_KEY") or getpass.getpass(
        "Enter master password: "
    )
    if not master_key:
        logger.error("Master password is required (set MXBIRDGE_MASTER_KEY env or enter interactively)")
        sys.exit(1)

    had_encrypted_fields = _config_needs_key(config)

    if had_encrypted_fields:
        try:
            config = decrypt_config(config, master_key)
            logger.info("Config decrypted successfully")
        except ValueError as e:
            logger.error("Config decryption failed: %s", e)
            sys.exit(1)

    if not had_encrypted_fields and _has_plaintext_credentials(config):
        _check_config_writable(config_path)
        config = _auto_encrypt_plaintext_fields(config, master_key, config_path)
        config = decrypt_config(config, master_key)

    try:
        config = await setup_credentials(config, config_path, master_password=master_key)
    except (ValueError, RuntimeError) as e:
        logger.error("Credential setup failed: %s", e)
        sys.exit(1)

    source_config = config.get("source", {})
    target_config = config.get("target", {})
    bridge_config = config.get("bridge", {})

    is_backup_mode = "target" not in config

    if not source_config.get("homeserver"):
        logger.error("source.homeserver is required in config")
        sys.exit(1)

    if not is_backup_mode and not target_config.get("homeserver"):
        logger.error("source.homeserver and target.homeserver are required in config")
        sys.exit(1)

    command_prefix = bridge_config.get("command_prefix", "!send")

    message_store = None
    store_cfg = bridge_config.get("message_store", {})
    if store_cfg.get("enabled", False):
        store_path = store_cfg.get("path", "messages.db")
        message_store = MessageStore(
            store_path,
            media_dir=store_cfg.get("media_dir", ""),
            db_password=master_key,
        )
        logger.info("Message store initialized: %s", store_path)

    if is_backup_mode:
        if not message_store:
            logger.error("Backup mode requires message_store to be enabled")
            sys.exit(1)
        logger.log(ALWAYS, "Running in backup mode (no target, messages will be saved only)")

    state = StateManager(bridge_config.get("state_path", "state.json"))
    await state.load()

    source = MatrixSourceBackend("source", source_config, state, config_path=config_path)

    target = None
    if not is_backup_mode:
        target = MatrixTargetBackend("target", target_config, state, command_prefix=command_prefix, config_path=config_path)

    core = BridgeCore(source, target, bridge_config, state=state, message_store=message_store)

    web_server = None
    web_cfg = bridge_config.get("web", {})
    if web_cfg.get("enabled", False):
        if message_store:
            from bridge.web import WebServer
            media_dir = store_cfg.get("media_dir", "")
            web_server = WebServer(message_store, web_cfg, media_dir=media_dir, full_config=config)
            logger.info("Web interface configured on %s:%s", web_cfg.get("host", "0.0.0.0"), web_cfg.get("port", 8080))
        else:
            logger.warning("Web interface enabled but message_store is not; skipping web server. Enable message_store to use the web interface.")

    loop = asyncio.get_running_loop()
    stop_event = asyncio.Event()

    def _signal_handler() -> None:
        logger.log(ALWAYS, "Received shutdown signal")
        stop_event.set()

    for sig in (signal.SIGINT, signal.SIGTERM):
        loop.add_signal_handler(sig, _signal_handler)

    async def _run_bridge():
        try:
            if web_server:
                try:
                    await web_server.start()
                except Exception as e:
                    logger.error("Web server failed to start, continuing without it: %s", e)
            await core.start()
            await core.run()
        except Exception as e:
            logger.log(ALWAYS, "Bridge crashed: %s", e, exc_info=True)

    bridge_task = asyncio.create_task(_run_bridge())

    done, pending = await asyncio.wait(
        [bridge_task, asyncio.create_task(stop_event.wait())],
        return_when=asyncio.FIRST_COMPLETED,
    )

    if bridge_task in done and not stop_event.is_set():
        logger.error("Bridge exited unexpectedly")

    stop_event.set()
    logger.log(ALWAYS, "Shutting down...")

    bridge_task.cancel()
    try:
        await bridge_task
    except asyncio.CancelledError:
        pass

    await core.stop()
    if web_server:
        await web_server.stop()
    await state.flush()
    if message_store:
        message_store.close()
    logger.log(ALWAYS, "Bridge stopped cleanly")


if __name__ == "__main__":
    asyncio.run(main())
