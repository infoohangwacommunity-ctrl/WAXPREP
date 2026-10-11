# Key Management and Rotation Procedure

This document describes how WaxPrep master keys are managed, rotated,
and recovered. It applies the OWASP [Cryptographic Storage Cheat
Sheet](https://cheatsheetseries.owasp.org/cheatsheets/Cryptographic_Storage_Cheat_Sheet.html)
and [Key Management Cheat
Sheet](https://cheatsheetseries.owasp.org/cheatsheets/Key_Management_Cheat_Sheet.html).

## 1. Key storage

- **Never commit master keys to Git.** The `.env.example` file documents
  the `WAXPREP_MASTER_KEYS` and `WAXPREP_ACTIVE_KEY_ID` variables but
  contains no real keys.
- Master keys live in a managed secrets service (AWS Secrets Manager,
  GCP Secret Manager, HashiCorp Vault, or equivalent). The application
  loads them via environment variables at startup.
- The keys must never be stored in the same database they protect.
- Backups of the keys must be encrypted with a separate backup key and
  access-restricted. Test recovery by restoring from backup in a
  separate environment at least once per quarter.

## 2. Key format

Each master key is `id:base64(32 random bytes)`. The id is a 1–255
integer; the active key id is set with `WAXPREP_ACTIVE_KEY_ID`.
Multiple keys may be present simultaneously:

```text
WAXPREP_MASTER_KEYS="1:AAAA...,2:BBBB..."
WAXPREP_ACTIVE_KEY_ID=2
```

The first key in the ring (id `1`) is the blind-index key. Changing it
invalidates all blind indexes; re-index from history after rotating id
1. (The blind-index key is intentionally a separate concern from the
field-encryption key, but the implementation reuses the same key for
both purposes when only one key is present — see
`src/waxprep/security/cipher.py` for details.)

## 3. Adding a new key (rotation in place)

1. Generate a new key: `python -c "import os,base64;print('2:'+base64.b64encode(os.urandom(32)).decode())"`
2. Add it to `WAXPREP_MASTER_KEYS` alongside the existing key(s).
3. Set `WAXPREP_ACTIVE_KEY_ID=2`.
4. Restart the application (or trigger a config reload).
5. New writes use key 2. Old writes can still be read because key 1
   remains in the ring.

The key id is stored in the first two bytes of every ciphertext blob
(`[format_version, key_id, nonce, ciphertext_and_tag]`). Decryption
picks the key by id; old rows remain readable forever.

## 4. Re-encrypting old rows with the new key (deep rotation)

Step 3 only changes which key is used for new writes. To re-encrypt
existing rows:

1. Verify a backup of the database and of every master key.
2. Run a re-encryption script that, for each student, decrypts every
   field with the old key and re-encrypts with the new key. The script
   must process one student at a time so RLS scopes the work.
3. After the script completes, remove the old key from
   `WAXPREP_MASTER_KEYS`.
4. Test that decryption still works without the old key.

> **Note:** A re-encryption script is not shipped with this release.
> It is documented here so it can be implemented before rotation is
> needed. Losing the only decryption key makes the protected history
> unrecoverable.

## 5. Losing a key (recovery)

- If the active key is lost but old keys remain, the data written under
  the lost key is unrecoverable. There is no fail-closed fallback.
- If the active key is lost and no fallback exists, the application
  must refuse to start (fail-closed). The cipher raises
  `DecryptionError` for unrecognized key ids; the storage layer
  propagates the error. Blank or fabricated context is forbidden.
- Test backup recovery at least once per quarter by deleting the live
  key and restoring from backup in a separate environment.

## 6. Blind-index key (rotation)

The blind-index key derives the HMAC key used by `search_tokens`. If
the blind-index key changes, every existing token hash is invalid. To
rotate:

1. Re-generate `WAXPREP_MASTER_KEYS` with a new id-1 key.
2. Run the `backfill_embedding_jobs` equivalent for tokens: for each
   history entry, re-compute the HMAC hashes and re-write the
   `search_tokens` rows. (This script is not shipped; implement before
   rotating.)
3. Old hashes are stale and will not match queries until re-indexed.
   Consider running both old and new hashes during the transition.

## 7. Audit

- Log every key rotation (timestamp, who, old key id, new key id).
- Log every key-recovery exercise.
- Review access logs for the secrets service monthly.
- Verify quarterly that backups can be restored in a separate
  environment.
