"""Select the Labeling projection after the caller has authorized its Project."""

from __future__ import annotations

import asyncio

from fastapi import HTTPException
import httpx

from aap_labeling.class_queries import (
    LabelingClassQueryManager,
    materialize_labeling_class_query_data,
)
from aap_labeling.class_query_contract import (
    LabelingClassQueryUnavailable,
    decode_labeling_class_query_reply,
    labeling_class_query_request,
)
from aap_labeling.class_query_postgres import LabelingClassQueryPostgresData
from aap_labeling.config import secret_source

from app.config import settings
from app.services.labeling_class_query_data import LabelingClassQueryData


class LabelingClassQueryOwnerInternalError(RuntimeError):
    """An explicit owner retains the original public plain-500 failure path."""


async def execute_labeling_class_query(db, *, project_id):
    request = labeling_class_query_request(project_id)
    try:
        url = settings.labeling_class_query_url
        try:
            dsn = secret_source(
                settings.labeling_class_query_database_url,
                settings.labeling_class_query_database_url_file,
            )
        except ValueError:
            raise LabelingClassQueryUnavailable() from None
        if url and dsn:
            raise LabelingClassQueryUnavailable()
        if not url and not dsn:
            reply = await LabelingClassQueryManager(LabelingClassQueryData(db)).execute(
                request
            )
        else:
            timeout = settings.labeling_class_query_timeout_seconds
            async with asyncio.timeout(timeout):
                if url:
                    try:
                        token = secret_source(
                            settings.labeling_class_query_control_token,
                            settings.labeling_class_query_control_token_file,
                        )
                    except ValueError:
                        raise LabelingClassQueryUnavailable() from None
                    if not token:
                        raise LabelingClassQueryUnavailable()
                    async with httpx.AsyncClient(
                        trust_env=False, timeout=timeout
                    ) as client:
                        response = await client.post(
                            url.rstrip("/") + "/v1/labeling/class-queries/execute",
                            headers={"Authorization": f"Bearer {token}"},
                            json=request.model_dump(mode="json", by_alias=True),
                        )
                    if response.status_code == 500:
                        raise LabelingClassQueryOwnerInternalError(
                            "Labeling class-query owner failed"
                        )
                    if response.status_code != 200:
                        raise LabelingClassQueryUnavailable()
                    reply = decode_labeling_class_query_reply(request, response.content)
                else:
                    reply = await LabelingClassQueryManager(
                        LabelingClassQueryPostgresData(dsn, command_timeout=timeout)
                    ).execute(request)
        return materialize_labeling_class_query_data(reply)
    except (
        LabelingClassQueryUnavailable,
        httpx.HTTPError,
        httpx.InvalidURL,
        OSError,
        TimeoutError,
    ):
        raise HTTPException(status_code=503, detail="标注统计服务暂不可用") from None
