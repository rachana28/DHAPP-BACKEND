"""
Saved-card logic: validate → tokenize → persist only the safe handle.

The raw PAN/CVV are validated offline (Luhn + expiry + brand) and handed to the
gateway tokenization middleware. ONLY ``card_token, fingerprint, last4, brand,
expiry`` (+ minimal metadata) are stored — the card number and CVV never touch
the database or the logs.
"""

from __future__ import annotations

from fastapi import HTTPException
from sqlmodel import Session, select

from app.core.models import SavedCard, SavedCardCreate, User
from app.modules.payments import gateway
from app.services.audit_log import emit_event as audit_emit
from app.utils import card_utils
from app.utils.id_generator import generate_reference_id, CARD
from app.utils.time_utils import now_ist


def active_cards(session: Session, user_id):
    return session.exec(
        select(SavedCard).where(
            SavedCard.user_id == user_id,
            SavedCard.is_active == True,  # noqa: E712
        )
    ).all()


def unset_other_defaults(session: Session, user_id, keep_id) -> None:
    for c in active_cards(session, user_id):
        if c.id != keep_id and c.is_default:
            c.is_default = False
            c.updated_at = now_ist()
            session.add(c)


def save_card(session: Session, user: User, data: SavedCardCreate) -> SavedCard:
    digits = "".join(ch for ch in (data.card_number or "") if ch.isdigit())
    if len(digits) < 12 or len(digits) > 19 or not card_utils.luhn_valid(digits):
        raise HTTPException(400, "Invalid card number")
    if not card_utils.validate_expiry(data.expiry_month, data.expiry_year):
        raise HTTPException(400, "Card has an invalid or past expiry date")
    if not (data.cvv and data.cvv.isdigit() and len(data.cvv) in (3, 4)):
        raise HTTPException(400, "Invalid CVV")

    # Hand off to the gateway vault — raw PAN/CVV are consumed and discarded here.
    vault = gateway.tokenize_card(
        digits,
        data.expiry_month,
        data.expiry_year,
        data.cvv,
        data.card_holder_name,
    )

    # De-dupe: same physical card already saved & active for this user.
    existing = session.exec(
        select(SavedCard).where(
            SavedCard.user_id == user.id,
            SavedCard.card_fingerprint == vault["fingerprint"],
            SavedCard.is_active == True,  # noqa: E712
        )
    ).first()
    if existing:
        raise HTTPException(409, "This card is already saved")

    is_first = len(active_cards(session, user.id)) == 0
    make_default = data.is_default or is_first

    card = SavedCard(
        reference_id=generate_reference_id(session, CARD),
        user_id=user.id,
        card_token=vault["card_token"],
        card_fingerprint=vault["fingerprint"],
        last4=vault["last4"],
        brand=vault["brand"],
        expiry_month=data.expiry_month,
        expiry_year=card_utils.normalize_year(data.expiry_year),
        card_holder_name=data.card_holder_name,
        nickname=data.nickname,
        is_default=make_default,
    )
    session.add(card)
    session.flush()  # assign card.id before clearing other defaults

    if make_default:
        unset_other_defaults(session, user.id, keep_id=card.id)

    session.commit()
    session.refresh(card)

    audit_emit(
        "card.saved",
        trip_id=None,
        actor="user",
        actor_id=str(user.id),
        payload={
            "card_reference": card.reference_id,
            "brand": card.brand,
            "last4": card.last4,
        },
    )
    return card
