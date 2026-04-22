#!/usr/bin/env python3
from __future__ import annotations

import getpass
import sys

from bridge.crypto import decrypt, encrypt, is_encrypted


def cmd_encrypt() -> None:
    plaintext = input("Value to encrypt: ").strip()
    if not plaintext:
        print("Error: empty value")
        sys.exit(1)

    key = getpass.getpass("Master password: ")
    key_confirm = getpass.getpass("Confirm password: ")
    if key != key_confirm:
        print("Error: passwords do not match")
        sys.exit(1)

    encrypted = encrypt(plaintext, key)
    print()
    print(f"Encrypted value:\n{encrypted}")
    print()
    print("Add this to config.yaml:")
    print(f'  access_token: "{encrypted}"')
    print(f'  password: "{encrypted}"')


def cmd_decrypt() -> None:
    encrypted = input("Encrypted value (enc:...): ").strip()
    if not is_encrypted(encrypted):
        print("Error: value does not start with 'enc:' prefix")
        sys.exit(1)

    key = getpass.getpass("Master password: ")
    plaintext = decrypt(encrypted, key)
    if plaintext is None:
        print("Error: decryption failed. Wrong master password?")
        sys.exit(1)

    print()
    print(f"Decrypted value:\n{plaintext}")


def main() -> None:
    if len(sys.argv) < 2:
        print("Usage:")
        print(f"  {sys.argv[0]} encrypt    — encrypt a value for config.yaml")
        print(f"  {sys.argv[0]} decrypt    — decrypt a value from config.yaml")
        sys.exit(1)

    command = sys.argv[1].lower()
    if command == "encrypt":
        cmd_encrypt()
    elif command == "decrypt":
        cmd_decrypt()
    else:
        print(f"Unknown command: {command}")
        print("Use 'encrypt' or 'decrypt'")
        sys.exit(1)


if __name__ == "__main__":
    main()
