"""Fault-path ownership checks for the explicit local rollback qualification."""

from contextlib import asynccontextmanager
from pathlib import Path
from types import SimpleNamespace
import unittest
from unittest.mock import patch

from verify_legacy_storage_rollback import Accounts, WorktreeError


class Owner:
    def __init__(self):
        self.statements = []
        self.exists = True

    @asynccontextmanager
    async def begin(self):
        yield self

    async def exec_driver_sql(self, statement):
        self.statements.append(statement)
        if statement.startswith("DROP ROLE"):
            self.exists = False

    async def scalar(self, *_args):
        return self.exists or None


class CleanupTests(unittest.IsolatedAsyncioTestCase):
    def account(self):
        settings = SimpleNamespace(
            minio_use_ssl=False,
            minio_endpoint="127.0.0.1:9000",
            minio_access_key="test-admin",
            minio_secret_key="private-password",
        )
        with patch("app.config.settings", settings):
            account = Accounts(Path("/private/owned-test"), SimpleNamespace())
        account.db_created = account.s3_created = True
        return account

    async def test_unknown_minio_cleanup_still_revokes_owned_database_role(self):
        account, owner = self.account(), Owner()

        def unavailable(*_args, **_kwargs):
            raise WorktreeError("temporary transport failure")

        account.mc = unavailable
        with self.assertRaisesRegex(WorktreeError, "MinIO service account"):
            await account.close(owner)
        self.assertTrue(account.s3_created)
        self.assertFalse(account.db_created)
        self.assertEqual(len(owner.statements), 2)
        self.assertIn(account.role, owner.statements[1])

    async def test_absent_account_is_idempotent_and_never_removes_another_id(self):
        account, owner = self.account(), Owner()
        calls = []

        def absent(*args, **_kwargs):
            calls.append(args)
            return SimpleNamespace(
                returncode=1,
                stdout=b'{"error":{"message":"The specified service account does not exist."}}',
            )

        account.mc = absent
        await account.close(owner)
        await account.close(owner)
        self.assertFalse(account.s3_created or account.db_created)
        self.assertEqual(len(owner.statements), 2)
        self.assertTrue(
            all(args == ("info", "rollback", account.access_key) for args in calls)
        )

    async def test_transport_failure_is_not_proof_of_account_removal(self):
        account, owner = self.account(), Owner()
        account.mc = lambda *_args, **_kwargs: SimpleNamespace(
            returncode=1, stdout=b'{"error":{"message":"connection refused"}}'
        )
        with self.assertRaisesRegex(WorktreeError, "MinIO service account"):
            await account.close(owner)
        self.assertTrue(account.s3_created)
        self.assertFalse(account.db_created)


if __name__ == "__main__":
    unittest.main()
