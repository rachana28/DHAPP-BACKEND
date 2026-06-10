"""
Payout admin controls.

Cash-out (provider-initiated) lives in the provider-wallet router; this router
holds only the admin-triggered manual sweep that auto-pays every provider's
positive wallet balance to their bank in one run (the same job the daily
scheduler runs).
"""

from __future__ import annotations

from fastapi import APIRouter, Depends
from sqlmodel import Session

from app.core.database import get_session
from app.core.security import get_current_admin
from app.core.models import User
from app.modules.payout import service as payout_service

router = APIRouter(prefix="/payout", tags=["Payout"])


@router.post("/admin/run-sweep")
def run_payout_sweep(
    session: Session = Depends(get_session),
    admin: User = Depends(get_current_admin),
):
    """Manually trigger the provider payout sweep (admin only)."""
    refs = payout_service.sweep_all_providers(session)
    return {"message": "Payout sweep complete", "payouts": refs, "count": len(refs)}
