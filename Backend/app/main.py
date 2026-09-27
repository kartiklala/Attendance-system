"""FastAPI application entry point.

Run locally:
    uvicorn app.main:app --reload --port 8000
"""
import logging
import sys

from fastapi import FastAPI, Request
from fastapi.exceptions import RequestValidationError
from fastapi.middleware.cors import CORSMiddleware
from fastapi.responses import JSONResponse

from app.core.config import settings
from app.core.firebase import init_firebase, is_available
from app.routers import admin, attendance, auth
from app.services.errors import APIError

# ---- Logging ------------------------------------------------------------
logging.basicConfig(
    level=logging.INFO,
    format="%(asctime)s %(levelname)s [%(name)s] %(message)s",
    stream=sys.stdout,
)
# Quiet noisy third-party loggers.
logging.getLogger("firebase_admin").setLevel(logging.WARNING)
logging.getLogger("google").setLevel(logging.WARNING)
logger = logging.getLogger("app")

# ---- App ----------------------------------------------------------------
app = FastAPI(
    title="Student Attendance System API",
    version="1.0.0",
    docs_url=None,          # keep the API surface minimal in production
    redoc_url=None,
    openapi_url="/openapi.json" if not settings.is_production else None,
)

app.add_middleware(
    CORSMiddleware,
    allow_origins=settings.frontend_origins,  # never "*" in production
    allow_credentials=True,                    # required for HttpOnly cookies
    allow_methods=["GET", "POST"],
    allow_headers=["Authorization", "Content-Type"],
)

app.include_router(auth.router)
app.include_router(attendance.router)
app.include_router(admin.router)


# ---- Exception handlers (consistent error shape, no internals leaked) ----
@app.exception_handler(APIError)
async def api_error_handler(_: Request, exc: APIError) -> JSONResponse:
    return JSONResponse(
        status_code=exc.status_code,
        content={"success": False, "error": {"code": exc.code, "message": exc.message}},
    )


@app.exception_handler(RequestValidationError)
async def validation_error_handler(_: Request, exc: RequestValidationError) -> JSONResponse:
    return JSONResponse(
        status_code=422,
        content={
            "success": False,
            "error": {"code": "VALIDATION_ERROR", "message": "Invalid request data."},
        },
    )


@app.exception_handler(Exception)
async def unhandled_error_handler(_: Request, exc: Exception) -> JSONResponse:
    logger.exception("Unhandled server error")
    return JSONResponse(
        status_code=500,
        content={
            "success": False,
            "error": {"code": "INTERNAL_ERROR", "message": "An unexpected error occurred."},
        },
    )


# ---- Startup / health -----------------------------------------------------
@app.on_event("startup")
def on_startup() -> None:
    if not settings.JWT_SECRET_KEY:
        logger.error("JWT_SECRET_KEY is not set — /authorize-user will fail until configured.")
    firebase_ok = init_firebase()
    logger.info(
        "Startup complete. firebase=%s sheet=%s env=%s",
        "ok" if firebase_ok else "NOT CONFIGURED",
        "ok" if settings.GOOGLE_SHEET_ID and settings.GOOGLE_SHEET_ID != "CHANGE_ME"
        else "NOT CONFIGURED",
        settings.APP_ENV,
    )


@app.get("/health")
def health() -> dict:
    return {
        "status": "ok",
        "service": "attendance-api",
        "firebase": "connected" if is_available() else "not_configured",
    }
