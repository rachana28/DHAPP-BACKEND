"""
Scheduled payout sweep.

Runs daily (registered in app.main lifespan) and moves every provider's positive
wallet balance to their registered bank account via the payout partner. Each run
opens its own DB session, mirroring the other scheduler jobs.
"""

from __future__ import annotations

from sqlmodel import Session

from app.core.database import engine
from app.modules.payout import service as payout_service


def auto_payout_sweep_scheduler() -> None:
    print("💸 Running daily provider payout sweep...")
    with Session(engine) as session:
        try:
            refs = payout_service.sweep_all_providers(session)
            if refs:
                print(f"✅ Auto-paid {len(refs)} provider wallet(s).")
        except Exception as e:  # pragma: no cover - defensive
            print(f"❌ Error in payout sweep: {e}")
