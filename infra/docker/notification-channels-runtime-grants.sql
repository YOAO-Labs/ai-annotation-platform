GRANT USAGE ON SCHEMA yoao_notifications_v1, yoao_labeling_v1, yoao_diagnostics_v1
    TO :"application_role";

GRANT EXECUTE ON FUNCTION
    yoao_notifications_v1.list_notification_channels(boolean),
    yoao_notifications_v1.get_notification_channel(text),
    yoao_notifications_v1.register_notification_channel(jsonb),
    yoao_notifications_v1.transition_notification_channel(jsonb),
    yoao_notifications_v1.open_notification_channel_secret(text),
    yoao_notifications_v1.list_user_channel_bindings(uuid),
    yoao_notifications_v1.upsert_user_channel_binding(jsonb),
    yoao_notifications_v1.mark_user_channel_binding_verified(uuid,text,timestamptz),
    yoao_notifications_v1.delete_user_channel_binding(uuid,text),
    yoao_notifications_v1.enqueue_channel_deliveries(jsonb),
    yoao_notifications_v1.has_terminal_channel_delivery(uuid,text,uuid),
    yoao_notifications_v1.channel_delivery_policy(),
    yoao_notifications_v1.claim_channel_deliveries(jsonb),
    yoao_notifications_v1.finish_channel_delivery(jsonb),
    yoao_labeling_v1.notification_target_visible_to_actor(uuid,text,uuid,uuid,uuid),
    yoao_diagnostics_v1.bug_report_visible_to_actor(uuid,uuid)
    TO :"application_role";
