"""
OncoKB annotation proxy - forwards mutation annotation requests to OncoKB.

Uses the public API (public.api.oncokb.org) when no token is configured,
which returns oncogenicity, mutation effect, hotspot, geneSummary, and
variantSummary for free. Set ONCOKB_API_TOKEN for the licensed API
(adds treatment/therapy data).

This endpoint exists so the WSI viewer (different origin from cBioPortal)
can obtain OncoKB annotations without CORS issues.
"""

from __future__ import annotations

import logging
from typing import Any, Optional

import httpx
from fastapi import APIRouter, HTTPException
from pydantic import BaseModel

from .config import settings

log = logging.getLogger(__name__)

router = APIRouter()

ONCOKB_PUBLIC_URL = (
    "https://public.api.oncokb.org/api/v1/annotate/mutations/byProteinChange"
)
ONCOKB_LICENSED_URL = (
    "https://www.oncokb.org/api/v1/annotate/mutations/byProteinChange"
)
ONCOKB_PUBLIC_CNA_URL = (
    "https://public.api.oncokb.org/api/v1/annotate/copyNumberAlterations"
)
ONCOKB_LICENSED_CNA_URL = (
    "https://www.oncokb.org/api/v1/annotate/copyNumberAlterations"
)


class _Gene(BaseModel):
    entrezGeneId: int


class OncoKbMutationItem(BaseModel):
    id: str
    alteration: str
    consequence: Optional[str] = None
    gene: _Gene
    proteinStart: Optional[int] = None
    proteinEnd: Optional[int] = None
    tumorType: Optional[str] = None


class OncoKbCopyNumberItem(BaseModel):
    id: str
    copyNameAlterationType: str
    gene: _Gene
    referenceGenome: str = "GRCh37"
    tumorType: Optional[str] = None


async def _post_oncokb(
    items: list[dict[str, Any]],
    *,
    public_url: str,
    licensed_url: str,
) -> Any:
    token = settings.oncokb_api_token
    url = licensed_url if token else public_url
    headers: dict[str, str] = {
        "Content-Type": "application/json",
        "Accept": "application/json",
    }
    if token:
        headers["Authorization"] = f"Bearer {token}"

    try:
        async with httpx.AsyncClient(timeout=15) as client:
            resp = await client.post(url, headers=headers, json=items)
    except httpx.TimeoutException:
        raise HTTPException(status_code=504, detail="OncoKB request timed out.")
    except httpx.RequestError as exc:
        log.warning("OncoKB request failed: %s", exc)
        raise HTTPException(status_code=502, detail="Could not reach OncoKB API.")

    if not resp.is_success:
        log.warning("OncoKB returned %s: %s", resp.status_code, resp.text[:200])
        raise HTTPException(
            status_code=resp.status_code,
            detail=f"OncoKB API error ({resp.status_code}).",
        )

    return resp.json()


@router.post("/api/oncokb/annotate")
async def oncokb_annotate(items: list[OncoKbMutationItem]) -> Any:
    if not items:
        return []
    return await _post_oncokb(
        [item.model_dump() for item in items],
        public_url=ONCOKB_PUBLIC_URL,
        licensed_url=ONCOKB_LICENSED_URL,
    )


@router.post("/api/oncokb/annotate-copy-number")
async def oncokb_annotate_copy_number(items: list[OncoKbCopyNumberItem]) -> Any:
    if not items:
        return []
    return await _post_oncokb(
        [item.model_dump() for item in items],
        public_url=ONCOKB_PUBLIC_CNA_URL,
        licensed_url=ONCOKB_LICENSED_CNA_URL,
    )
