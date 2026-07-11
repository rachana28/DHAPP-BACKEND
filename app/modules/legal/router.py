"""Public legal & company content API (prefix /api/v1/legal).

Serves database-managed documents (terms, policies, company info) to the user
and provider apps, including pre-login screens, so no authentication is
required. Read-only, Redis-cached, rate-limited, strictly whitelisted fields.
Company identity tokens like [APP NAME] are resolved at response time from
the LegalPlaceholder table. There is intentionally no write API anywhere for
this content: documents, sections, and placeholder values change only by
direct SQL, and edits reach the apps when the caches expire.
"""

from typing import Any, Dict, List, Optional

from fastapi import APIRouter, Depends, HTTPException, Path, Query
from sqlmodel import Session, select

from app.core.cache import cache_get_json, cache_set_json
from app.core.database import get_session
from app.core.models import (
    LegalDocument,
    LegalDocumentPublicDetailResponse,
    LegalDocumentPublicResponse,
    LegalDocumentSection,
    LegalPlaceholder,
    LegalSectionPublicResponse,
)
from app.core.rate_limit import RateLimiter

LEGAL_CACHE_TTL = 300
PLACEHOLDER_CACHE_TTL = 60
PLACEHOLDER_CACHE_KEY = "legal:placeholders"

router = APIRouter(prefix="/api/v1/legal", tags=["Legal Content"])


def legal_list_key(audience: str, language: str) -> str:
    return f"legal:docs:{audience}:{language}"


def legal_detail_key(slug: str, language: str) -> str:
    return f"legal:doc:{slug}:{language}"


def _placeholder_map(session: Session) -> Dict[str, str]:
    cached = cache_get_json(PLACEHOLDER_CACHE_KEY)
    if isinstance(cached, dict):
        return cached
    rows = session.exec(select(LegalPlaceholder)).all()
    mapping = {row.key: row.value for row in rows}
    cache_set_json(PLACEHOLDER_CACHE_KEY, mapping, PLACEHOLDER_CACHE_TTL)
    return mapping


def _resolve_text(text: str, mapping: Dict[str, str]) -> str:
    for key, value in mapping.items():
        if value and value.strip():
            text = text.replace(f"[{key}]", value)
    return text


def _resolve_document(
    payload: Dict[str, Any], mapping: Dict[str, str]
) -> Dict[str, Any]:
    payload["title"] = _resolve_text(payload["title"], mapping)
    for section in payload.get("sections", []):
        section["heading"] = _resolve_text(section["heading"], mapping)
        section["body"] = _resolve_text(section["body"], mapping)
    return payload


def _active_documents(
    session: Session, audience: str, language: str
) -> List[LegalDocument]:
    q = select(LegalDocument).where(
        LegalDocument.is_active,
        LegalDocument.language == language,
    )
    if audience != "all":
        q = q.where(LegalDocument.audience.in_([audience, "all"]))
    q = q.order_by(LegalDocument.display_order, LegalDocument.id)
    return session.exec(q).all()


def _active_document(
    session: Session, slug: str, language: str
) -> Optional[LegalDocument]:
    return session.exec(
        select(LegalDocument).where(
            LegalDocument.slug == slug,
            LegalDocument.language == language,
            LegalDocument.is_active,
        )
    ).first()


@router.get(
    "/documents",
    response_model=List[LegalDocumentPublicResponse],
    dependencies=[Depends(RateLimiter(times=30, seconds=60))],
)
def list_legal_documents(
    audience: str = Query("all", pattern="^(user|provider|all)$"),
    language: str = Query("en", pattern="^[a-z]{2}(-[A-Z]{2})?$"),
    session: Session = Depends(get_session),
):
    key = legal_list_key(audience, language)
    payload = cache_get_json(key)
    if payload is None:
        docs = _active_documents(session, audience, language)
        if not docs and language != "en":
            docs = _active_documents(session, audience, "en")
        payload = [
            LegalDocumentPublicResponse.model_validate(doc).model_dump(mode="json")
            for doc in docs
        ]
        cache_set_json(key, payload, LEGAL_CACHE_TTL)
    mapping = _placeholder_map(session)
    for item in payload:
        item["title"] = _resolve_text(item["title"], mapping)
    return payload


@router.get(
    "/documents/{slug}",
    response_model=LegalDocumentPublicDetailResponse,
    dependencies=[Depends(RateLimiter(times=30, seconds=60))],
)
def get_legal_document(
    slug: str = Path(min_length=1, max_length=80, pattern="^[a-z0-9-]+$"),
    language: str = Query("en", pattern="^[a-z]{2}(-[A-Z]{2})?$"),
    session: Session = Depends(get_session),
):
    key = legal_detail_key(slug, language)
    payload = cache_get_json(key)
    if payload is None:
        doc = _active_document(session, slug, language)
        if doc is None and language != "en":
            doc = _active_document(session, slug, "en")
        if doc is None:
            raise HTTPException(status_code=404, detail="Document not found")
        sections = session.exec(
            select(LegalDocumentSection)
            .where(
                LegalDocumentSection.document_id == doc.id,
                LegalDocumentSection.is_active,
            )
            .order_by(LegalDocumentSection.display_order, LegalDocumentSection.id)
        ).all()
        payload = LegalDocumentPublicResponse.model_validate(doc).model_dump(
            mode="json"
        )
        payload["sections"] = [
            LegalSectionPublicResponse.model_validate(s).model_dump(mode="json")
            for s in sections
        ]
        cache_set_json(key, payload, LEGAL_CACHE_TTL)
    return _resolve_document(payload, _placeholder_map(session))
