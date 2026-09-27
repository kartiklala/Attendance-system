"""Authentication routes: POST /authorize-user (+ POST /logout, GET /me)."""
import logging

from fastapi import APIRouter, Depends, Request, Response

from app.core.security import apply_auth_cookie, clear_auth_cookie, create_application_jwt
from app.middleware.auth import CurrentUser, get_current_user
from app.models.auth import AuthorizeResponse, AuthorizedUser
from app.services import auth_service
from app.services.errors import UnauthorizedError

logger = logging.getLogger(__name__)

router = APIRouter(tags=["auth"])


def _bearer_token(request: Request) -> str:
    header = request.headers.get("Authorization", "")
    scheme, _, token = header.partition(" ")
    if scheme.lower() != "bearer" or not token.strip():
        raise UnauthorizedError("Missing Firebase ID token. Send 'Authorization: Bearer <token>'.")
    return token.strip()


@router.post("/authorize-user", response_model=AuthorizeResponse)
def authorize_user(request: Request, response: Response) -> AuthorizeResponse:
    """Verify the Firebase ID token, decide the role server-side, and issue
    the 3-minute application JWT in an HttpOnly cookie."""
    identity = auth_service.verify_firebase_id_token(_bearer_token(request))
    role = auth_service.determine_role(identity["email"])
    auth_service.upsert_user(identity["uid"], identity["name"], identity["email"], role)

    token = create_application_jwt(identity["uid"], role, identity["email"])
    apply_auth_cookie(response, token)

    logger.info("User authorized role=%s (uid verified)", role)
    user = AuthorizedUser(
        uid=identity["uid"], name=identity["name"], email=identity["email"], role=role
    )
    return AuthorizeResponse(success=True, user=user)


@router.post("/logout")
def logout(response: Response) -> dict:
    clear_auth_cookie(response)
    return {"success": True}


@router.get("/me")
def me(user: CurrentUser = Depends(get_current_user)) -> dict:
    return {"success": True, "user": {"uid": user.uid, "email": user.email, "role": user.role}}
