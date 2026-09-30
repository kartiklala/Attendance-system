"""Application configuration loaded from environment variables (.env)."""
import os
from pathlib import Path

from dotenv import load_dotenv

BACKEND_DIR = Path(__file__).resolve().parents[2]
load_dotenv(BACKEND_DIR / ".env")


def _float(name: str, default: float) -> float:
    try:
        return float(os.getenv(name, default))
    except (TypeError, ValueError):
        return default


def _int(name: str, default: int) -> int:
    try:
        return int(os.getenv(name, default))
    except (TypeError, ValueError):
        return default


class Settings:
    APP_ENV: str = os.getenv("APP_ENV", "development")

    # Application JWT
    JWT_SECRET_KEY: str = os.getenv("JWT_SECRET_KEY", "")
    JWT_EXPIRE_MINUTES: int = _int("JWT_EXPIRE_MINUTES", 3)
    JWT_ALGORITHM: str = "HS256"
    AUTH_COOKIE_NAME: str = "attendance_jwt"

    # Attendance rules
    ATTENDANCE_RADIUS_METERS: float = _float("ATTENDANCE_RADIUS_METERS", 30)
    STUDENT_SESSION_MINUTES: int = _int("STUDENT_SESSION_MINUTES", 1)
    QR_TOKEN_LIFETIME_SECONDS: int = _int("QR_TOKEN_LIFETIME_SECONDS", 10)
    # Sentinel lifetime meaning "never auto-expire": the QR stays valid until
    # the CR manually refreshes it (POST /qr/rotate). 0 is falsy, so every
    # read of this field must be None-safe (never `value or default`).
    QR_PERMANENT_LIFETIME_SECONDS: int = 0
    # CR-adjustable QR lifetimes (seconds). Backend validates every request
    # against this list — the frontend can never pick an unsafe value.
    QR_ALLOWED_LIFETIME_SECONDS: tuple[int, ...] = (5, 10, 15, 20, 30, 60)
    # Sentinel lifetime meaning "never expires". A Permanent QR is only rotated
    # when the CR presses the manual refresh button (POST /qr/rotate), which
    # immediately invalidates the previous token.
    QR_PERMANENT_LIFETIME_SECONDS: int = 0

    # ---- Shared-browser (proxy) detection ---------------------------------
    # Pepper for the one-way browser identifier. Deliberately NOT a required new
    # environment variable: it falls back to JWT_SECRET_KEY so an existing
    # deployment keeps working unchanged, while allowing a dedicated secret.
    BROWSER_ID_PEPPER: str = os.getenv("BROWSER_ID_PEPPER", "")
    # Marks on one browser closer together than this look like one person
    # working through a list, so the suspicion is raised to "high".
    PROXY_SHORT_WINDOW_SECONDS: int = _int("PROXY_SHORT_WINDOW_SECONDS", 20)
    # A Google session younger than this counts as "just signed in", which
    # supports a shared-browser conclusion (a real class stays signed in for
    # hours or days). Derived ONLY from the signature-verified Firebase token.
    PROXY_FRESH_SIGNIN_SECONDS: int = _int("PROXY_FRESH_SIGNIN_SECONDS", 900)
    # How many trusted proxies sit in front of the app (Render = 1). The client
    # IP is read this far from the RIGHT of X-Forwarded-For, because the
    # left-most entry is whatever value the client chose to send.
    TRUSTED_PROXY_COUNT: int = _int("TRUSTED_PROXY_COUNT", 1)

    # CORS / URLs
    FRONTEND_URL: str = os.getenv("FRONTEND_URL", "http://localhost:5173")
    PUBLIC_APP_URL: str = os.getenv("PUBLIC_APP_URL", FRONTEND_URL)

    # Credentials
    FIREBASE_CREDENTIALS_FILE: str = os.getenv("FIREBASE_CREDENTIALS_FILE", "")
    GOOGLE_SERVICE_ACCOUNT_FILE: str = os.getenv("GOOGLE_SERVICE_ACCOUNT_FILE", "")

    # Google Sheets
    GOOGLE_SHEET_ID: str = os.getenv("GOOGLE_SHEET_ID", "")
    GOOGLE_SHEET_NAME: str = os.getenv("GOOGLE_SHEET_NAME", "Attendance")
    GOOGLE_STUDENTS_SHEET_NAME: str = os.getenv("GOOGLE_STUDENTS_SHEET_NAME", "Sheet1")
    # Bounded HTTP timeout (seconds) for Google Sheets calls, so a slow Sheets
    # read fails fast and the roster cache (stale-on-error) can cover it,
    # instead of hanging on the OS default and surfacing as a 500.
    SHEETS_TIMEOUT_SECONDS: float = _float("SHEETS_TIMEOUT_SECONDS", 20)

    @property
    def browser_id_secret(self) -> str:
        """Pepper for browser identifiers: never the raw id, never reversible."""
        return self.BROWSER_ID_PEPPER or self.JWT_SECRET_KEY

    @property
    def is_production(self) -> bool:
        return self.APP_ENV.strip().lower() == "production"

    @property
    def frontend_origins(self) -> list[str]:
        return [o.strip() for o in self.FRONTEND_URL.split(",") if o.strip()]

    def resolve_path(self, path_str: str) -> Path:
        """Resolve a possibly-relative path against the backend directory."""
        p = Path(path_str)
        return p if p.is_absolute() else (BACKEND_DIR / p)


settings = Settings()
