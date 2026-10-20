"""Identity group capabilities on the caller's existing transaction."""

from __future__ import annotations

import json
import uuid
from contextlib import asynccontextmanager

from sqlalchemy import text
from sqlalchemy.exc import SQLAlchemyError
from sqlalchemy.orm.attributes import set_committed_value
from fastapi import HTTPException

from aap_identity.contract import IdentityUnavailable
from aap_identity.group_contract import (
    decode_native_group_command,
    decode_native_group_reply,
)
from app.db.models.user import User
from app.services.identity_session_data import _json_value


class IdentityGroupCommandData:
    def __init__(self, db):
        self.db = db

    @asynccontextmanager
    async def transaction(self):
        try:
            await self.db.flush()
            yield self
            await self.db.commit()
        except SQLAlchemyError:
            await self.db.rollback()
            raise IdentityUnavailable() from None
        except BaseException:
            await self.db.rollback()
            raise

    async def execute_group_command(self, payload: dict) -> dict:
        try:
            command = decode_native_group_command(payload)
            await self.db.flush()
            raw = _json_value(
                await self.db.scalar(
                    text(
                        "SELECT aap_identity_groups_v1.execute_group_command("
                        "CAST(:command AS jsonb))"
                    ),
                    {
                        "command": json.dumps(
                            command.model_dump(mode="json", by_alias=True),
                            ensure_ascii=False,
                            allow_nan=False,
                        )
                    },
                )
            )
            reply = decode_native_group_reply(command, raw)
            account = getattr(reply.data, "account", None)
            if account is not None:
                for instance in list(self.db.identity_map.values()):
                    if isinstance(instance, User) and instance.id == account.id:
                        for name in ("group_id", "group_name"):
                            set_committed_value(instance, name, getattr(account, name))
            return raw
        except SQLAlchemyError:
            await self.db.rollback()
            raise IdentityUnavailable() from None
        except BaseException:
            await self.db.rollback()
            raise


async def resolve_invitation_group(db, invitation_id: uuid.UUID, token: str):
    """Keep the legacy acceptance transaction while Identity owns group rows."""
    try:
        raw = _json_value(
            await db.scalar(
                text(
                    "SELECT aap_identity_groups_v1.resolve_invitation_group("
                    ":invitation_id, :token)"
                ),
                {"invitation_id": invitation_id, "token": token},
            )
        )
    except SQLAlchemyError as error:
        await db.rollback()
        if getattr(getattr(error, "orig", None), "sqlstate", None) in {
            "55P03",
            "40001",
            "40P01",
        }:
            raise HTTPException(
                status_code=409, detail={"reason": "group_busy"}
            ) from None
        raise HTTPException(status_code=503, detail="身份分组服务暂不可用") from None
    if raw is None:
        return None
    if raw == {"error": {"code": "invitation_invalid"}}:
        raise HTTPException(status_code=410, detail="邀请已失效，请联系管理员重发邀请")
    if raw == {"error": {"code": "invalid_group_name"}}:
        raise HTTPException(
            status_code=409,
            detail="邀请中的数据组名称无效，请联系管理员重发邀请",
        )
    try:
        if (
            not isinstance(raw, dict)
            or set(raw) != {"id", "name"}
            or not isinstance(raw["name"], str)
            or not 1 <= len(raw["name"]) <= 100
        ):
            raise ValueError()
        return {"id": uuid.UUID(raw["id"]), "name": raw["name"]}
    except (ValueError, TypeError, AttributeError):
        await db.rollback()
        raise HTTPException(status_code=503, detail="身份分组服务暂不可用") from None
