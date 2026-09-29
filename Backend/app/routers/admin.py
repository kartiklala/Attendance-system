"""Admin routes: manage the authoritative CR list (POST/GET/DELETE /admin/cr).

The role of the caller is verified from the backend-signed application JWT —
a student or CR crafting this request manually is rejected (ADMIN_ONLY).
Writes go through this backend only; the React app never touches Firestore.
"""
import logging
import re
from datetime import datetime, timezone

from fastapi import APIRouter, Depends
from pydantic import BaseModel

from app.middleware.auth import CurrentUser, require_admin
from app.models.auth import MessageResponse
from app.core.firebase import COLLECTION_ADMIN_LIST, get_db
from app.services import auth_service
from app.services.errors import BadRequestError, ConflictError, NotFoundError

logger = logging.getLogger(__name__)

router = APIRouter(tags=["admin"])

MAX_EMAIL_LEN = 254
# Doc ids are the normalized email; Firestore forbids "/" so we substitute.
DOC_ID_SAFE_RE = re.compile(r"[/.#\[\]$*]")


class AddCRRequest(BaseModel):
    email: str

    def normalized(self) -> str:
        return auth_service.normalize_email(self.email)


@router.get("/admin/cr")
def list_crs(admin: CurrentUser = Depends(require_admin)) -> dict:
    """Admin-only: current CR emails from the authoritative admin_list."""
    emails = sorted(
        (doc.to_dict() or {}).get("email", "")
        for doc in get_db().collection(COLLECTION_ADMIN_LIST).stream()
    )
    return {"success": True, "cr_emails": [e for e in emails if e]}


@router.post("/admin/cr")
def add_cr(
    payload: AddCRRequest,
    admin: CurrentUser = Depends(require_admin),
) -> MessageResponse:
    """Add (or refresh) a CR email in the existing admin_list collection.

    Idempotent: the document id is the normalized email, so duplicate
    submissions cannot create duplicate entries.
    """
    email = payload.normalized()
    if len(email) > MAX_EMAIL_LEN or not auth_service.is_valid_email(email):
        raise BadRequestError("Please enter a valid email address.",
                              code="INVALID_EMAIL")
    if auth_service.is_admin_email(email):
        raise ConflictError("That email is registered as an admin, not a CR.",
                            code="EMAIL_IS_ADMIN")

    db = get_db()
    ref = db.collection(COLLECTION_ADMIN_LIST).document(DOC_ID_SAFE_RE.sub("_", email))
    now_iso = datetime.now(timezone.utc).isoformat()
    existing = ref.get()
    if existing.exists:
        # Already a CR — refresh metadata instead of failing with duplicates.
        ref.set({"email": email, "updated_at": now_iso}, merge=True)
        message = "That CR email already exists — it has been refreshed."
    else:
        ref.set({
            "email": email,
            "created_by": admin.uid,
            "created_at": now_iso,
        })
        message = "CR added successfully."
    logger.info("CR email added to admin_list by admin uid")
    return MessageResponse(success=True, message=message)


@router.delete("/admin/cr/{email}", response_model=MessageResponse)
def remove_cr(
    email: str,
    admin: CurrentUser = Depends(require_admin),
) -> MessageResponse:
    """Admin-only: remove a CR email from the authoritative admin_list.

    The document id is the normalized email (same scheme as add_cr), so the
    lookup is exact. Removing a non-existent CR is a 404 rather than silent."""
    target = auth_service.normalize_email(email)
    if not target or not auth_service.is_valid_email(target):
        raise BadRequestError("Please enter a valid email address.",
                              code="INVALID_EMAIL")
    db = get_db()
    ref = db.collection(COLLECTION_ADMIN_LIST).document(DOC_ID_SAFE_RE.sub("_", target))
    if not ref.get().exists:
        raise NotFoundError("That CR email was not found.", code="CR_NOT_FOUND")
    ref.delete()
    logger.info("CR email removed from admin_list by admin uid")
    return MessageResponse(success=True, message="CR removed successfully.")
