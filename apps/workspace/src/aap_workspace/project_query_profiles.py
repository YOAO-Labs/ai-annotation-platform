"""Lazy standalone dependency on the Identity-owned Briefs public client."""

from .config import secret_source
from .project_queries import ProjectQueryProfilesUnavailable


class ProjectQueryIdentityProfiles:
    def __init__(self, settings):
        self.settings = settings

    async def resolve(self, ids):
        if not ids:
            return {}
        from aap_identity.briefs_client import BriefsClient
        from aap_identity.briefs_contract import BriefsFailure, decode_briefs_request
        from aap_identity.briefs_postgres import BriefsPostgresData
        from aap_identity.contract import IdentityUnavailable

        try:
            settings = self.settings
            timeout = settings.project_query_identity_timeout_seconds
            url = settings.project_query_identity_url
            if url:
                token = secret_source(
                    settings.project_query_identity_token,
                    settings.project_query_identity_token_file,
                )
                client = BriefsClient(
                    identity_url=url, identity_token=token, timeout_seconds=timeout
                )
            else:
                dsn = secret_source(
                    settings.project_query_identity_briefs_database_url,
                    settings.project_query_identity_briefs_database_url_file,
                )
                if not dsn:
                    raise IdentityUnavailable()
                client = BriefsClient(
                    data_factory=lambda: BriefsPostgresData(dsn),
                    timeout_seconds=timeout,
                )
            request = decode_briefs_request(
                {
                    "schema": "aap-identity-briefs/v1",
                    "operation": "get_user_briefs",
                    "payload": {"user_ids": [str(value) for value in ids]},
                }
            )
            reply = await client.execute(request)
            return {str(user.id): user for user in reply.data.users}
        except (
            IdentityUnavailable,
            BriefsFailure,
            ValueError,
            TypeError,
            UnicodeError,
            OSError,
            TimeoutError,
        ):
            raise ProjectQueryProfilesUnavailable() from None
