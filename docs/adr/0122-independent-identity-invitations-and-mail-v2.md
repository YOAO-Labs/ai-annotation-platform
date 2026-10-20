# 0122 — Identity 邀请生命周期与 durable MailV2 投递所有权

- **Status:** Accepted; implementation is in progress and runtime qualification is pending
- **Date:** 2026-10-20
- **Deciders:** Maintainers
- **Supersedes:** None
- **Extends:** ADR-0118, ADR-0119, ADR-0120 and ADR-0121

## Context

Invitation management, registration and acceptance still span API ORM workflows,
Identity rules, Group creation, Workspace membership and Audit. Moving only the
invitation service would leave the proof, account and project-membership writes
split across owners. Sending an invitation email also needs to preserve the
existing public response and link while surviving a worker restart or an
uncertain SMTP result. Recovery mail already has a versioned owner and must
remain usable while invitation delivery evolves independently.

This decision covers the complete 13-operation invitation lifecycle and a
separate durable mail sender. It does not complete the Identity module, user
directory, platform-role administration, all offboarding, production deployment
or a replacement-language provider.

## Decision

### Invitation owner and compatibility

Keep the existing public API paths, request/response schemas, SDK fields, HTTP
statuses, token/link response fields and CSV/JSON representation. The API
adapts them to private `POST /v1/identity/invitations/execute` and the closed
`aap-identity-invitations/v1` value protocol. The thirteen operations are
`create_invitation`, `preview_invitation_batch`, `apply_invitation_batch`,
`list_invitations`, `query_invitations`, `invitation_stats`,
`export_invitations`, `revoke_invitation`, `resend_invitation`,
`send_invitation_email`, `resolve_invitation`, `register_invitation` and
`accept_invitation`.

`identity_invitation_client` has only three LOGIN capabilities:
`invitation_policy()`, `prepare_invitation_command(jsonb)` and
`apply_invitation_command(jsonb)`. The portable owner lives in
`apps/identity/src/aap_identity/{invitation_contract,invitation_manager,invitation_postgres}.py`;
the API route and caller-transaction adapter remain at
`apps/api/app/api/v1/invitation_commands.py`,
`apps/api/app/services/identity_invitation_commands.py` and
`identity_invitation_data.py`. Direct table, sequence, DDL, AUTH, GROUP and
MAIL permissions are not part of this LOGIN.

Current platform and project roles remain distinct. Project administrators
manage their own invitations and may not request global scope; only a current
super-admin may issue privileged platform-role invitations. Project invitations
use annotator, reviewer or viewer membership roles, subject to current platform
role compatibility. An accepted invitation does not change an existing
account's platform role. Existing public 404/410/409 distinctions remain.

Create, resolve, send and accept recheck current issuer, credential and project
authority. Workspace membership and owner facts are captured and revalidated
under the same PostgreSQL transaction as User, Group, invitation proof and Audit
writes. Lock order is sorted User authority, email advisory key, Project, then
Invitation/ProjectMember. Workspace owner changes cannot cross the final write
fence. Batch preview rolls back its entire outer transaction; apply retains
per-item savepoints and ordered results. Existing query limits, filters, quota,
TTL, error text and response rules are preserved.

The invitation token column remains the authoritative proof; there is no proof
hash migration or second plaintext receipt copy. Successful command receipts
are bound to operation identity, actor, request HMAC and derivation epoch, and
do not use live User or Invitation foreign keys. Secrets and SQL diagnostics do
not enter errors, Audit details or logs.

Migration 0242 installs, in order,
`0242_identity_invitations_support.sql`,
`0242_invitation_mail.sql` and `0242_identity_invitations.sql`. The grant source
is `infra/docker/identity-invitations-grants.sql`. Group resolution remains a
private helper inside Identity; the older API `identity_group_data.py` remains
the caller-side Group adapter, while only its legacy invitation-resolution
helper retires with the old acceptance coordinator.

### Durable invitation delivery

Retain public `send-email` success/failure behavior: 200 when sent, 502 for
terminal failure or bounded pending, and send the current invitation link. Do
not return public 202 or rotate/revoke the invitation when SMTP fails. The
invitation owner commits a durable intent and exposes private
`mail_status=pending|sent|failed`; the API polls the same operation UUID for a
bounded interval (default 60 seconds, maximum 120). A timeout can return 502
while the accepted intent remains eligible for later delivery.

Use separate `aap-identity-mail-native/v2` and
`aap-identity-mail/v2` contracts with the four-function
`identity_mail_v2_client`: `mail_policy()`, `claim_mail(jsonb)`,
`renew_mail(jsonb)` and `finish_mail(jsonb)`. The V2 sender supports recovery
and invitation purposes; the existing v1 Recovery contract, role and worker
remain valid and filter invitation rows. Both protocols use the existing
credential mail outbox with typed V2 leases and a closed invitation context.
The invitation outbox `user_id` continues to identify the issuer, not the
recipient.

The sender runs as `python -m aap_identity_runtime.mail_worker_v2` from the same
Identity artifact. It receives only its MAILV2 DSN, MAIL_SETTINGS DSN, Fernet
key and SMTP configuration; it has no AUTH, invitation, control or JWT signing
secret. It commits claim/renew/finish transactions before and after SMTP, never
holding SQL locks across network I/O. Stable Message-ID and at-least-once retry
allow duplicate delivery after an uncertain SMTP result; the invitation link
remains valid. Fernet key rotation must retain prior keys while pending messages
still require decryption. Optional local Compose uses the `mail-v2` profile and
the `identity-mail-v2` service. Producer SMTP credentials are not introduced.

## Consequences

Positive:

- Invitation, account, project membership, token consumption and Audit retain a
  single transaction and a current-authority write fence.
- Existing clients keep their public contract while durable mail survives
  bounded API waits and sender restarts.
- Recovery V1 remains compatible and receives no invitation rows or new
  capability grants.

Negative:

- Invitation and MailV2 add separate database capabilities, schemas, grants,
  leases and deployment configuration that must be operated together.
- SMTP is at-least-once; a disconnect after DATA can cause a duplicate message,
  and a 502 timeout does not prove that the message will not arrive later.
- Accepted design and local source implementation do not establish migration,
  role, API-consumer, restart, backup/restore, capacity or production
  qualification. Those checks remain pending.

## Notes

- Implementation sources: `apps/identity/src/aap_identity/invitation_contract.py`,
  `invitation_manager.py`, `invitation_postgres.py`, `mail_v2_contract.py` and
  `mail_v2_postgres.py`; runtime entrypoint `aap_identity_runtime.mail_worker_v2`.
- API adapter: `apps/api/app/services/identity_invitation_commands.py` and
  `identity_invitation_data.py`.
- Deployment details: [Identity private runtime protocol](../../docs-site/api/guides/identity-runtime.md#邀请生命周期)
  and the repository's `docker-compose.identity.yml`.
- Full-module progress remains in the
  [backend decoupling plan](../plans/1792160873_backend-module-decoupling.md).
