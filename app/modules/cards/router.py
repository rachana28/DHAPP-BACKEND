"""
Saved cards (user app).

  GET    /cards          list the user's active saved cards (brand + last4 only)
  POST   /cards          add a card (PAN tokenized; only safe fields stored)
  PATCH  /cards/{ref}     update nickname / holder name / default
  DELETE /cards/{ref}     soft-delete (token retained for audit/refunds)

Responses NEVER include the card token — only brand, last4 and expiry.
"""

from __future__ import annotations

from typing import List

from fastapi import APIRouter, Depends, HTTPException
from sqlmodel import Session, select

from app.core.database import get_session
from app.core.models import (
    User,
    SavedCard,
    SavedCardCreate,
    SavedCardUpdate,
    SavedCardPublic,
)
from app.core.security import get_current_user
from app.modules.cards import service as card_service
from app.utils.id_generator import get_by_reference
from app.utils.time_utils import now_ist

router = APIRouter(prefix="/cards", tags=["Cards"])


def _owned_active(session: Session, reference_id: str, user_id) -> SavedCard:
    card = get_by_reference(session, SavedCard, reference_id)
    if not card or card.user_id != user_id or not card.is_active:
        raise HTTPException(404, "Card not found")
    return card


@router.get("", response_model=List[SavedCardPublic])
def list_cards(
    session: Session = Depends(get_session),
    current_user: User = Depends(get_current_user),
):
    rows = session.exec(
        select(SavedCard)
        .where(
            SavedCard.user_id == current_user.id,
            SavedCard.is_active == True,  # noqa: E712
        )
        .order_by(SavedCard.is_default.desc(), SavedCard.id.desc())
    ).all()
    return rows


@router.post("", response_model=SavedCardPublic)
def add_card(
    data: SavedCardCreate,
    session: Session = Depends(get_session),
    current_user: User = Depends(get_current_user),
):
    return card_service.save_card(session, current_user, data)


@router.patch("/{reference_id}", response_model=SavedCardPublic)
def update_card(
    reference_id: str,
    data: SavedCardUpdate,
    session: Session = Depends(get_session),
    current_user: User = Depends(get_current_user),
):
    card = _owned_active(session, reference_id, current_user.id)
    update_data = data.model_dump(exclude_unset=True)

    if "card_holder_name" in update_data:
        card.card_holder_name = update_data["card_holder_name"]
    if "nickname" in update_data:
        card.nickname = update_data["nickname"]

    if update_data.get("is_default") is True:
        card.is_default = True
        card_service.unset_other_defaults(session, current_user.id, keep_id=card.id)
    elif update_data.get("is_default") is False:
        card.is_default = False

    card.updated_at = now_ist()
    session.add(card)
    session.commit()
    session.refresh(card)
    return card


@router.delete("/{reference_id}")
def delete_card(
    reference_id: str,
    session: Session = Depends(get_session),
    current_user: User = Depends(get_current_user),
):
    card = _owned_active(session, reference_id, current_user.id)
    was_default = card.is_default
    card.is_active = False
    card.is_default = False
    card.updated_at = now_ist()
    session.add(card)

    # Keep one default selected if other cards remain.
    if was_default:
        remaining = [
            c
            for c in card_service.active_cards(session, current_user.id)
            if c.id != card.id
        ]
        if remaining:
            promote = max(remaining, key=lambda c: c.id)
            promote.is_default = True
            promote.updated_at = now_ist()
            session.add(promote)

    session.commit()
    return {"status": "deleted", "id": reference_id}
