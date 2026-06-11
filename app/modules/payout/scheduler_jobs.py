"""
Scheduled payout sweep.

Interval-driven with a persisted due-time gate (22:30 IST, marker stored as a
SystemConfig row): moves every provider's positive wallet balance to their
registered bank account via the payout partner. If the server was down at the
due minute, the next tick after restart runs the missed day exactly once —
``execute_payout`` is idempotent per wallet per day. Each run opens its own DB
session, mirroring the other scheduler jobs.
"""

from __future__ import annotations

from sqlmodel import Session

from app.core.database import engine
from app.modules.payout import service as payout_service
from app.utils.system_config import daily_job_due, mark_job_run


def auto_payout_sweep_scheduler() -> None:
    with Session(engine) as session:
        try:
            if not daily_job_due(session, "job_last_run_auto_payout_sweep", 22, 30):
                return
            print("💸 Running daily provider payout sweep...")
            refs = payout_service.sweep_all_providers(session)
            if refs:
                print(f"✅ Auto-paid {len(refs)} provider wallet(s).")
            mark_job_run(session, "job_last_run_auto_payout_sweep")
        except Exception as e:
            print(f"❌ Error in payout sweep: {e}")
