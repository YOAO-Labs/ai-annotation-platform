"""Inspect or remove expired/unreachable auxiliary publication artifacts."""

import argparse
import asyncio
import json

from app.db.base import async_session
from app.services.point_cloud_auxiliary.reconcile import reconcile_auxiliary


async def run(args):
    async with async_session() as db:
        result = await reconcile_auxiliary(
            db,
            apply=args.apply,
            limit=args.limit,
            grace_hours=args.grace_hours,
            cursor=args.cursor,
        )
        if args.apply:
            await db.commit()
        print(json.dumps(result, indent=2))


def main():
    parser = argparse.ArgumentParser(description=__doc__)
    parser.add_argument("--apply", action="store_true")
    parser.add_argument("--cursor", help="Continuation from the previous next_cursor")
    parser.add_argument("--limit", type=int, default=100, choices=range(1, 1001))
    parser.add_argument("--grace-hours", type=int, default=24, choices=range(2, 8761))
    asyncio.run(run(parser.parse_args()))


if __name__ == "__main__":
    main()
