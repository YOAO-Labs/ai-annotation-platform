# Notifications preference protocol peer

This small Node client calls the versioned PostgreSQL preference function directly. It is a local interoperability check for the same borrowed-transaction capability used by the API; it is not a separate Notifications HTTP service.

Run `pnpm install --ignore-workspace --frozen-lockfile --ignore-scripts`, then `pnpm run check` for the schema self-check. The owned database verifier supplies `NOTIFICATION_PREFERENCES_DATABASE_URL` and `NOTIFICATION_PREFERENCE_USER_ID` when it runs the live owner check.
