import logging
from typing import Literal

from pydantic import BaseModel, ConfigDict, Field

from taskqueue.redis_client import get_redis
from workers.context import JobContext
from workers.registry import job_handler

logger = logging.getLogger(__name__)

SENT_KEY = "taskflow:notification:sent:{notification_id}"
SENT_TTL_SECONDS = 7 * 24 * 3600


class SendNotificationPayload(BaseModel):
    model_config = ConfigDict(extra="forbid")

    # Caller-chosen id that makes delivery idempotent across retries and duplicate messages.
    notification_id: str = Field(min_length=1, max_length=100)
    channel: Literal["email", "sms", "webhook"]
    recipient: str = Field(min_length=1, max_length=255)
    message: str = Field(min_length=1, max_length=2000)


def _mask(recipient: str) -> str:
    return recipient[:2] + "***" if len(recipient) > 2 else "***"


@job_handler("send_notification", SendNotificationPayload, timeout_seconds=30)
def send_notification(payload: SendNotificationPayload, ctx: JobContext) -> dict:
    ctx.check()
    key = SENT_KEY.format(notification_id=payload.notification_id)
    # SET NX acts as the delivery idempotency guard: only the first execution "sends".
    first_delivery = get_redis().set(key, str(ctx.job_id), nx=True, ex=SENT_TTL_SECONDS)
    if not first_delivery:
        return {"notification_id": payload.notification_id, "delivered": True, "deduplicated": True}

    # A real implementation would call the provider here, passing notification_id
    # as the provider's idempotency key.
    logger.info(
        "Notification sent via %s to %s", payload.channel, _mask(payload.recipient),
        extra={"job_id": ctx.job_id},
    )
    return {"notification_id": payload.notification_id, "delivered": True, "deduplicated": False}
