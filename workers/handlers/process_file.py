import hashlib
from collections import Counter
from typing import Literal

from pydantic import BaseModel, ConfigDict, Field

from workers.context import JobContext
from workers.registry import job_handler


class ProcessFilePayload(BaseModel):
    model_config = ConfigDict(extra="forbid")

    file_name: str = Field(min_length=1, max_length=255)
    # Content is supplied inline. Fetching arbitrary client URLs from workers would be an SSRF risk.
    content: str = Field(max_length=50_000)
    operations: list[Literal["line_count", "word_count", "checksum", "top_words"]] = Field(
        default_factory=lambda: ["line_count", "word_count", "checksum"],
        min_length=1,
    )


@job_handler("process_file", ProcessFilePayload, timeout_seconds=60)
def process_file(payload: ProcessFilePayload, ctx: JobContext) -> dict:
    result: dict = {"file_name": payload.file_name}
    words = payload.content.split()
    for operation in payload.operations:
        ctx.check()
        if operation == "line_count":
            result["line_count"] = len(payload.content.splitlines())
        elif operation == "word_count":
            result["word_count"] = len(words)
        elif operation == "checksum":
            result["sha256"] = hashlib.sha256(payload.content.encode()).hexdigest()
        elif operation == "top_words":
            counts = Counter(word.lower().strip(".,;:!?\"'()") for word in words)
            result["top_words"] = [
                {"word": word, "count": count} for word, count in counts.most_common(10) if word
            ]
    return result
