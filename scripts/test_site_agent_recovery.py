"""The fault driver must fail before contacting non-test infrastructure."""

import unittest
from unittest.mock import Mock, patch

from site_agent_recovery import Recovery, rows


class RecoveryGuards(unittest.TestCase):
    def test_development_environment_cannot_observe_database(self):
        with patch.dict("os.environ", {"AAP_WORKTREE_MODE": "dev"}, clear=True):
            with self.assertRaisesRegex(ValueError, "test_mode_required"):
                rows("SELECT 1")

    def test_wrong_database_is_rejected_before_connection(self):
        environment = {
            "AAP_WORKTREE_MODE": "test",
            "DATABASE_URL": "postgresql+asyncpg://localhost/retained_dev",
        }
        with patch.dict("os.environ", environment, clear=True):
            with self.assertRaisesRegex(ValueError, "test_database_required"):
                rows("SELECT 1")

    def test_fault_injection_refuses_changed_container_ownership(self):
        fixture = object.__new__(Recovery)
        fixture.owner = Mock()
        fixture.owner.inspect.side_effect = ValueError("resource_owner_changed")
        fixture.agent, fixture.network = {"id": "agent"}, {"id": "network"}
        with self.assertRaisesRegex(ValueError, "resource_owner_changed"):
            fixture.stop_agent(disconnected=True)
        fixture.owner.action.assert_not_called()


if __name__ == "__main__":
    unittest.main()
