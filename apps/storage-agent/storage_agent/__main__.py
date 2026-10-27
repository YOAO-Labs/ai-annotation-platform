"""Standalone local checks, one-time pairing and authenticated outbound work."""

import argparse
import json
import os
import sqlite3
import sys

from aap_filesystem_read import FilesystemStorageError
from aap_site_agent_contract import ProtocolError

from .client import Client
from .config import AgentError, Config, Credentials
from .journal import Journal
from .pairing import pair
from .runner import Runner


def main():
    parser = argparse.ArgumentParser(description="Controlled outbound storage Agent")
    parser.add_argument("--config", required=True)
    mode = parser.add_mutually_exclusive_group()
    mode.add_argument(
        "--check",
        action="store_true",
        help="Validate local config only; does not pair with the center",
    )
    mode.add_argument("--once", action="store_true")
    mode.add_argument(
        "--drain",
        action="store_true",
        help="Submit pending results without taking new commands",
    )
    mode.add_argument(
        "--pairing-file",
        help="Pair once using a private 0600 JSON file; never retries automatically",
    )
    args = parser.parse_args()
    os.umask(0o077)
    try:
        config = Config.load(args.config)
        if args.check:
            print(
                json.dumps(
                    {
                        "protocol_version": 1,
                        "root_handles": sorted(config.roots),
                        "limits": config.public_limits(),
                    }
                )
            )
            return 0
        if args.pairing_file:
            pair(config, args.pairing_file)
            print("agent_paired")
            return 0
        credentials = Credentials.load(config.credential_file)
        with Journal(config.state_dir, max_pending=config.max_pending) as journal:
            journal.remove_orphans()
            runner = Runner(config, credentials, journal)
            Client(config, credentials).run(runner, once=args.once, drain=args.drain)
        return 0
    except (AgentError, ProtocolError, FilesystemStorageError) as exc:
        print(getattr(exc, "code", "agent_failed"), file=sys.stderr)
        return 2
    except (OSError, ValueError, sqlite3.Error):
        print("agent_local_state_unavailable", file=sys.stderr)
        return 2


if __name__ == "__main__":
    sys.exit(main())
