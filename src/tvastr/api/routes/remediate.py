"""On-demand remediation endpoint.

POST /remediate runs the full pipeline. With an empty body it replays the bundled
sample logs; otherwise it processes the events supplied in the request.
"""

from __future__ import annotations

from functools import lru_cache

from fastapi import APIRouter
from pydantic import BaseModel, Field

from tvastr.domain import LogEvent
from tvastr.pipeline import PipelineRun, RemediationPipeline, build_pipeline

router = APIRouter(tags=["remediation"])


@lru_cache
def _pipeline() -> RemediationPipeline:
    """Process-wide pipeline (compiles the agent graph once)."""
    return build_pipeline()


class RemediateRequest(BaseModel):
    events: list[LogEvent] | None = Field(
        default=None,
        description="Log events to analyze. Omit to replay the bundled sample logs.",
    )


@router.post("/remediate", response_model=PipelineRun)
def remediate(request: RemediateRequest) -> PipelineRun:
    return _pipeline().run(events=request.events)
