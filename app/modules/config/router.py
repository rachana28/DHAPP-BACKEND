from fastapi import APIRouter, Depends
from sqlmodel import Session, select
from datetime import datetime

from app.core.database import get_session
from app.core.models import UITheme, UIBanner

# Set up the router with a v1 prefix for future version control
router = APIRouter(prefix="/api/v1/app-config", tags=["App Configuration"])


@router.get("")
def get_app_config(session: Session = Depends(get_session)):
    """
    Fetches the dynamic UI configuration for the mobile app,
    including the active theme and scrolling banners.
    """
    now = datetime.utcnow()

    # Priority 1: Fetch active Festival based on time limit
    festival_stmt = select(UITheme).where(
        UITheme.theme_type == "FESTIVAL",
        UITheme.is_active,
        (UITheme.start_date <= now) | (UITheme.start_date.is_(None)),
        (UITheme.end_date >= now) | (UITheme.end_date.is_(None)),
    )
    active_theme = session.exec(festival_stmt).first()

    # Priority 2: Fallback to active Season if no festival is running
    if not active_theme:
        season_stmt = select(UITheme).where(
            UITheme.theme_type == "SEASON", UITheme.is_active
        )
        active_theme = session.exec(season_stmt).first()

    # Fetch active banners
    banner_stmt = select(UIBanner).where(
        UIBanner.is_active,
        (UIBanner.start_date <= now) | (UIBanner.start_date.is_(None)),
        (UIBanner.end_date >= now) | (UIBanner.end_date.is_(None)),
    )
    active_banners = session.exec(banner_stmt).all()

    return {
        "theme": {
            "name": active_theme.name if active_theme else "DEFAULT",
            "animation_style": active_theme.animation_style if active_theme else "NONE",
        },
        "banners": [
            {
                "id": b.id,
                "image_url": b.image_url,
                "title": b.title,
                "details": b.details_text,
                "route": b.action_route,
            }
            for b in active_banners
        ],
    }
