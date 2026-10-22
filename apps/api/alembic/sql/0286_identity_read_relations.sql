-- Identity-owned current account and invitation target read projections.
CREATE VIEW aap_identity_v1.account_profiles
WITH (security_barrier = true)
AS
SELECT
    account.id,
    account.name,
    account.email,
    account.role,
    account.is_active,
    account.status,
    account.avatar_ref
FROM public.users AS account;
-- aap-statement-break
REVOKE ALL ON aap_identity_v1.account_profiles FROM PUBLIC;
-- aap-statement-break
CREATE VIEW aap_identity_v1.invitation_audit_targets
WITH (security_barrier = true)
AS
SELECT
    invitation.id,
    invitation.project_id
FROM public.user_invitations AS invitation;
-- aap-statement-break
REVOKE ALL ON aap_identity_v1.invitation_audit_targets FROM PUBLIC;
