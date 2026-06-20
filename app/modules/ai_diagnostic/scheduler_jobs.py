"""Daily cleanup jobs for the AI Diagnostic gateway.

Wired into the existing AsyncIOScheduler in app/main.py at a 24h interval.
- Media sweep: Core owns media bytes -> delete orphaned objects older than 24h.
- Orphan row sweep: safety net only; the AI service runs the primary row sweep.
Both swallow and log errors so a transient failure never crashes the scheduler.
"""

from app.modules.ai_diagnostic import ai_client, storage


async def ai_media_sweep_job() -> None:
    """Delete orphaned AI media older than 24h from R2."""
    try:
        count = await storage.sweep_ai_media(24)
        if count:
            print(f"🧹 AI media sweep deleted {count} orphaned object(s).")
    except Exception as e:
        print(f"❌ AI media sweep failed: {e}")


async def ai_orphan_row_sweep_job() -> None:
    """Safety-net call to the AI service's own orphan-row sweep."""
    try:
        await ai_client.trigger_orphan_sweep()
    except Exception as e:
        print(f"❌ AI orphan row sweep failed: {e}")
