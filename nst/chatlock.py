"""Password lock for chat history, with encryption at rest.

The threat model is local: someone with access to this PC (or a backup of it)
reading the per-conversation JSON files under ``%LOCALAPPDATA%\\NetSplitTunnel``.
A password that only hid the UI would be theatre — the files would still be
plaintext — so a **locked conversation's history file is encrypted** with a key
derived from the password.

Crypto is **stdlib only** (no third-party dependency, so the app stays the
lightweight no-admin single-file install it is):

* ``key  = PBKDF2-HMAC-SHA256(password, salt, 200_000)`` split into enc + mac keys
* ``keystream = SHA256(enc_key || nonce || counter)`` blocks XORed with plaintext
* ``tag = HMAC-SHA256(mac_key, nonce || ciphertext)``   (encrypt-then-MAC)
* a fresh random 16-byte nonce per file; the tag is checked in constant time

This is a sound authenticated-encryption construction for the local threat model.
Losing the password makes locked history unrecoverable **by design** — so
"reset" deletes the locked chats (after the security questions are answered).

Scope:
* ``global``    — every conversation is locked; the app gates on launch.
* ``selective`` — only the conversations in :func:`locked_keys` are encrypted;
  each prompts for the password the first time it's opened in a session.
"""

import hashlib
import hmac
import secrets

from . import config

_MAGIC = b"NSTLK1"           # envelope marker for an encrypted history file
_ITERS = 200_000            # PBKDF2 iterations
_VERIFY_MSG = b"nst-lock-verify"

# Cached (enc_key, mac_key) for the unlocked session; None while locked.
_keys: tuple[bytes, bytes] | None = None


# ── key derivation ────────────────────────────────────────────────────────────
def _derive(password: str, salt: bytes) -> tuple[bytes, bytes]:
    dk = hashlib.pbkdf2_hmac("sha256", password.encode("utf-8"), salt, _ITERS, dklen=64)
    return dk[:32], dk[32:]


def _verifier(mac_key: bytes) -> str:
    return hmac.new(mac_key, _VERIFY_MSG, hashlib.sha256).hexdigest()


# ── password lifecycle ────────────────────────────────────────────────────────
def is_set() -> bool:
    """True if a lock password has been configured."""
    return bool(config.load_lock_salt() and config.load_lock_verifier())


def is_unlocked() -> bool:
    """True if the correct password has been supplied this session."""
    return _keys is not None


def needs_unlock() -> bool:
    return is_set() and not is_unlocked()


def set_password(password: str) -> bool:
    """Create (or replace) the lock password and unlock the session.

    Replacing an existing password assumes the caller re-saves locked history so
    it is re-encrypted with the new key.
    """
    global _keys
    if not password:
        return False
    salt = secrets.token_bytes(16)
    enc_key, mac_key = _derive(password, salt)
    config.save_lock_salt(salt.hex())
    config.save_lock_verifier(_verifier(mac_key))
    _keys = (enc_key, mac_key)
    return True


def unlock(password: str) -> bool:
    """Verify *password*; cache the keys for the session on success."""
    global _keys
    salt_hex = config.load_lock_salt()
    verifier = config.load_lock_verifier()
    if not salt_hex or not verifier:
        return False
    try:
        salt = bytes.fromhex(salt_hex)
    except ValueError:
        return False
    enc_key, mac_key = _derive(password, salt)
    if not hmac.compare_digest(_verifier(mac_key), verifier):
        return False
    _keys = (enc_key, mac_key)
    return True


def lock() -> None:
    """Drop the cached keys (re-lock without forgetting the password)."""
    global _keys
    _keys = None


def clear() -> None:
    """Forget the password and all lock settings entirely (caller re-saves
    locked history as plaintext / or it is deleted on reset)."""
    global _keys
    _keys = None
    config.clear_lock()


# ── scope ─────────────────────────────────────────────────────────────────────
def scope() -> str:
    return config.load_lock_scope()


def set_scope(value: str, keys: list[str] | None = None) -> None:
    config.save_lock_scope(value)
    if keys is not None:
        config.save_locked_chats(keys)


def locked_keys() -> set[str]:
    return set(config.load_locked_chats())


def is_locked(key: str) -> bool:
    """True if conversation *key* should be encrypted on disk."""
    if not key or not is_set():
        return False
    if scope() == "global":
        return True
    return key in locked_keys()


# ── security questions (gate the destructive reset) ───────────────────────────
def _norm(answer: str) -> str:
    return " ".join(answer.strip().lower().split())


def set_questions(qa: list[tuple[str, str]]) -> None:
    """Store security questions with salted hashes of their answers."""
    out = []
    for q, a in qa:
        if not q.strip() or not a.strip():
            continue
        salt = secrets.token_bytes(16)
        h = hashlib.pbkdf2_hmac("sha256", _norm(a).encode("utf-8"), salt, _ITERS)
        out.append({"q": q.strip(), "salt": salt.hex(), "hash": h.hex()})
    config.save_lock_questions(out)


def questions() -> list[str]:
    return [item.get("q", "") for item in config.load_lock_questions()]


def has_questions() -> bool:
    return bool(config.load_lock_questions())


def verify_answers(answers: list[str]) -> bool:
    """True if every stored security answer matches (case/space-insensitive)."""
    stored = config.load_lock_questions()
    if not stored or len(answers) != len(stored):
        return False
    ok = True
    for item, ans in zip(stored, answers):
        try:
            salt = bytes.fromhex(item.get("salt", ""))
            want = item.get("hash", "")
        except ValueError:
            return False
        h = hashlib.pbkdf2_hmac("sha256", _norm(ans).encode("utf-8"), salt, _ITERS)
        # Compare every entry (no early-out) to keep timing uniform.
        ok = hmac.compare_digest(h.hex(), want) and ok
    return ok


# ── envelope encryption ───────────────────────────────────────────────────────
def _crypt(enc_key: bytes, nonce: bytes, data: bytes) -> bytes:
    """SHA256 counter-mode keystream XORed with *data* (symmetric)."""
    out = bytearray(len(data))
    counter = 0
    for i in range(0, len(data), 32):
        block = hashlib.sha256(enc_key + nonce + counter.to_bytes(8, "big")).digest()
        chunk = data[i:i + 32]
        out[i:i + len(chunk)] = bytes(c ^ k for c, k in zip(chunk, block))
        counter += 1
    return bytes(out)


def is_blob(raw: bytes) -> bool:
    return raw[:len(_MAGIC)] == _MAGIC


def blob_key(raw: bytes) -> str:
    """Read the plaintext conversation key stored in an encrypted file's header."""
    try:
        off = len(_MAGIC)
        klen = int.from_bytes(raw[off:off + 2], "big")
        off += 2
        return raw[off:off + klen].decode("utf-8")
    except Exception:
        return ""


def encrypt_payload(key_name: str, raw: bytes) -> bytes:
    """Wrap serialized history *raw* in an encrypted envelope. Requires unlock."""
    if _keys is None:
        raise RuntimeError("chat lock is not unlocked")
    enc_key, mac_key = _keys
    nonce = secrets.token_bytes(16)
    ct = _crypt(enc_key, nonce, raw)
    tag = hmac.new(mac_key, nonce + ct, hashlib.sha256).digest()
    kb = key_name.encode("utf-8")
    return _MAGIC + len(kb).to_bytes(2, "big") + kb + nonce + tag + ct


def decrypt_payload(raw: bytes) -> bytes | None:
    """Verify + decrypt an envelope. Returns None on tamper / wrong key / locked."""
    if _keys is None or not is_blob(raw):
        return None
    enc_key, mac_key = _keys
    try:
        off = len(_MAGIC)
        klen = int.from_bytes(raw[off:off + 2], "big")
        off += 2 + klen
        nonce = raw[off:off + 16]
        off += 16
        tag = raw[off:off + 32]
        off += 32
        ct = raw[off:]
        expect = hmac.new(mac_key, nonce + ct, hashlib.sha256).digest()
        if not hmac.compare_digest(tag, expect):
            return None
        return _crypt(enc_key, nonce, ct)
    except Exception:
        return None
