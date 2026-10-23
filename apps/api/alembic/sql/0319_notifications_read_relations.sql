CREATE VIEW aap_notifications_v1.notification_facts
WITH (security_barrier = true)
AS
SELECT
    notification.id,
    notification.user_id,
    notification.type,
    notification.target_type,
    notification.target_id,
    notification.payload,
    notification.read_at,
    notification.created_at
FROM public.notifications AS notification;
-- aap-statement-break
REVOKE ALL ON aap_notifications_v1.notification_facts FROM PUBLIC;
