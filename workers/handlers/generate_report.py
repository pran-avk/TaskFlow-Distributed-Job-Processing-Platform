import csv
import hashlib
import io
import json
from typing import Literal

from pydantic import BaseModel, ConfigDict, Field

from workers.context import JobContext
from workers.registry import job_handler

CHUNK_SIZE = 500


class GenerateReportPayload(BaseModel):
    model_config = ConfigDict(extra="forbid")

    report_id: str = Field(min_length=1, max_length=100)
    format: Literal["csv", "json"] = "csv"
    rows: int = Field(default=1500, ge=1, le=100_000)


@job_handler("generate_report", GenerateReportPayload, timeout_seconds=120)
def generate_report(payload: GenerateReportPayload, ctx: JobContext) -> dict:
    """Builds a deterministic report. Re-running it yields the same output,
    so duplicate execution is harmless."""
    records = []
    for start in range(0, payload.rows, CHUNK_SIZE):
        ctx.check()
        for i in range(start, min(start + CHUNK_SIZE, payload.rows)):
            records.append({"row": i + 1, "report_id": payload.report_id, "value": (i * 7919) % 1000})

    if payload.format == "csv":
        buffer = io.StringIO()
        writer = csv.DictWriter(buffer, fieldnames=["row", "report_id", "value"])
        writer.writeheader()
        writer.writerows(records)
        content = buffer.getvalue()
    else:
        content = json.dumps(records)

    data = content.encode()
    return {
        "file_name": f"{payload.report_id}.{payload.format}",
        "records_processed": len(records),
        "size_bytes": len(data),
        "sha256": hashlib.sha256(data).hexdigest(),
    }
