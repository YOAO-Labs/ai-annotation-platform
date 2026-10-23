CREATE VIEW aap_workspace_v1.project_member_facts
WITH (security_barrier = true)
AS
SELECT
    member.id,
    member.project_id,
    member.user_id,
    member.role,
    member.assigned_by,
    member.assigned_at,
    member.version,
    member.updated_at,
    member.weekly_target
FROM public.project_members AS member;
-- aap-statement-break
REVOKE ALL ON aap_workspace_v1.project_member_facts FROM PUBLIC;
