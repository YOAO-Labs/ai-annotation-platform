# ADR-0103: Assets storage connection administration

- Status: Accepted
- Date: 2026-10-18
- Deciders: core team

## Context

Storage connection CRUD and bounded connectivity probes still use API ORM rows, API-local encryption and network clients. The independent connector importer already consumes Assets-owned encrypted snapshots through seven restricted native capabilities. Connection administration can move independently of an import transaction, but connection changes must continue to fence later importer credential reads. Host allowlist administration belongs to Settings.

## Decision

Reuse the existing Python Assets Source runtime and its portable crypto, host guard and S3/SFTP code. Publish six complete `aap-assets-connections/v1` operations: paged list, read, create, update, delete and test. Preserve public response shapes and add optional UUID idempotency keys to mutations and probes. API consumers pass actor identities and request values, never permission flags, encryption keys or fallback host policies.

Publish nine Assets native capabilities in migration 0217. Source LOGIN grows from fourteen to twenty-three functions without table, sequence or foreign-schema grants. The importer LOGIN retains its separate seven-function set; Catalog is unchanged. One immutable `storage_connection_operations` receipt table has no live-resource foreign keys. Mutations, Audit events and replay receipts commit or roll back together.

Creation still requires a current platform manager, with global creation restricted to superadmins. Current creators retain management of owner connections after a platform role change. Global use remains available to active authenticated users, while global management remains superadmin-only. Read/test hide inaccessible records as missing; update/delete preserve their management-denial behavior. Legacy `project_id` input remains ignored on create, and legacy stored scope is projected as owner scope when it is not global.

Null or omitted patch fields keep their values; supplied config and secrets replace their respective values. SFTP config changes validate the retained credential mode and infer absent `auth_type`. Captures use nonlocking Identity facts and perform no DML. Provider validation, DNS and connector I/O run before final locks. Final application checks current Identity, connection snapshot and, when network policy was used, the current Settings-owned host-policy snapshot. The Settings capability briefly locks the settings relation during final application so a previously absent override cannot race a concurrent insert; no network I/O occurs under that lock.

The Source runtime receives an explicit optional connector encryption key matching the existing importer key, an environment fallback allowlist and an optional known-hosts file. Existing source operations do not require the connector key. Fernet ciphertext stays within provider/native bindings. HTTP results contain only allowlisted public config and `secret_set`; historical endpoint userinfo and unsupported secret config fields are removed. Probes retain the twenty-entry sample, timeouts and SSH host-key rejection, and expose exception class names instead of SDK text.

Replay identity stores a keyed, operation-bound credential fingerprint rather than plaintext or an unsalted password digest. The key is the decoded thirty-two-byte Fernet key. The HMAC-SHA256 input is `aap-assets-connections-request/v1`, a NUL byte, the canonical lowercase operation UUID, another NUL byte and the existing sorted, compact, ASCII-escaped finite JSON credential encoding. Portable crypto fixtures freeze that profile. Request context and newly randomized Fernet ciphertext are not part of replay identity. Receipt reads still check the current active actor and relevant ownership/management rights before returning the first committed result. A new key requests a fresh probe.

Deleting a connection does not delete retained import operation snapshots or evidence. Subsequent importer credential reads keep their current `connection_changed` behavior; an already loaded lease retains its existing execution semantics. This does not change the separate rule that deleting referenced source assets returns 409 until project detachment.

## Consequences

The Assets provider owns encryption, probes and durable connection mutation. API remains the public authenticated gateway; allowlist and deployment-preset administration stay with Settings and deployment configuration. A different provider language must implement the frozen JSON/native contract and credential-intent fixtures, without loading the API or Python runtime.

This closes the connection administration slice. Non-spatial operator ingestion, derived media/Blob operations, remaining Assets capabilities and other modules still require extraction and qualification. Go remains an isolated experiment.

## References

- [Module deployment status](../../docs-site/dev/concepts/module-deployment.md)
- [Transactional native capabilities](0092-transactional-module-data-capabilities.md)
- [Independent Assets Source](../../docs-site/api/guides/assets-source.md)
