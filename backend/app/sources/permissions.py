"""Development source approvals never authorize production collection."""

from app.core.config import get_settings


def terms_permit_crawl(site) -> bool:
    if site.terms_status != "approved":
        return False
    return get_settings().env == "dev" or site.terms_approval_scope == "production"
