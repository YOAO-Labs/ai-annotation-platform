GRANT USAGE ON SCHEMA yoao_identity_v1 TO :"notifications_owner", :"labeling_owner", :"diagnostics_owner";
GRANT SELECT (id, role, is_active) ON yoao_identity_v1.account_profiles TO :"notifications_owner";
GRANT EXECUTE ON FUNCTION yoao_identity_v1.read_actor_facts(uuid) TO :"labeling_owner", :"diagnostics_owner";

GRANT USAGE, CREATE ON SCHEMA yoao_notifications_v1 TO :"notifications_owner";
ALTER TABLE yoao_notifications_v1.notification_channels OWNER TO :"notifications_owner";
ALTER TABLE yoao_notifications_v1.user_notification_channel_bindings OWNER TO :"notifications_owner";
ALTER TABLE yoao_notifications_v1.notification_channel_deliveries OWNER TO :"notifications_owner";
ALTER FUNCTION yoao_notifications_v1._preference_policy_v2() OWNER TO :"notifications_owner";
ALTER FUNCTION yoao_notifications_v1.preference_command(jsonb) OWNER TO :"notifications_owner";
ALTER FUNCTION yoao_notifications_v1._channel_public_projection(
    yoao_notifications_v1.notification_channels
) OWNER TO :"notifications_owner";
ALTER FUNCTION yoao_notifications_v1.list_notification_channels(boolean) OWNER TO :"notifications_owner";
ALTER FUNCTION yoao_notifications_v1.get_notification_channel(text) OWNER TO :"notifications_owner";
ALTER FUNCTION yoao_notifications_v1.register_notification_channel(jsonb) OWNER TO :"notifications_owner";
ALTER FUNCTION yoao_notifications_v1.transition_notification_channel(jsonb) OWNER TO :"notifications_owner";
ALTER FUNCTION yoao_notifications_v1.open_notification_channel_secret(text) OWNER TO :"notifications_owner";
ALTER FUNCTION yoao_notifications_v1.list_user_channel_bindings(uuid) OWNER TO :"notifications_owner";
ALTER FUNCTION yoao_notifications_v1.upsert_user_channel_binding(jsonb) OWNER TO :"notifications_owner";
ALTER FUNCTION yoao_notifications_v1.mark_user_channel_binding_verified(uuid,text,timestamptz)
    OWNER TO :"notifications_owner";
ALTER FUNCTION yoao_notifications_v1.delete_user_channel_binding(uuid,text) OWNER TO :"notifications_owner";
ALTER FUNCTION yoao_notifications_v1.enqueue_channel_deliveries(jsonb) OWNER TO :"notifications_owner";
ALTER FUNCTION yoao_notifications_v1.has_terminal_channel_delivery(uuid,text,uuid)
    OWNER TO :"notifications_owner";
ALTER FUNCTION yoao_notifications_v1.channel_delivery_policy() OWNER TO :"notifications_owner";
ALTER FUNCTION yoao_notifications_v1.claim_channel_deliveries(jsonb) OWNER TO :"notifications_owner";
ALTER FUNCTION yoao_notifications_v1.finish_channel_delivery(jsonb) OWNER TO :"notifications_owner";
REVOKE CREATE ON SCHEMA yoao_notifications_v1 FROM :"notifications_owner";

GRANT USAGE, CREATE ON SCHEMA yoao_labeling_v1 TO :"labeling_owner";
ALTER FUNCTION yoao_labeling_v1._notification_task_visible(uuid,text,uuid,uuid)
    OWNER TO :"labeling_owner";
ALTER FUNCTION yoao_labeling_v1.notification_target_visible_to_actor(uuid,text,uuid,uuid,uuid)
    OWNER TO :"labeling_owner";
REVOKE CREATE ON SCHEMA yoao_labeling_v1 FROM :"labeling_owner";

GRANT USAGE, CREATE ON SCHEMA yoao_diagnostics_v1 TO :"diagnostics_owner";
ALTER FUNCTION yoao_diagnostics_v1.bug_report_visible_to_actor(uuid,uuid)
    OWNER TO :"diagnostics_owner";
REVOKE CREATE ON SCHEMA yoao_diagnostics_v1 FROM :"diagnostics_owner";
