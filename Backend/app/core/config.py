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
    # CR-adjustable QR lifetimes (seconds). Backend validates every request
    # against this list — the frontend can never pick an unsafe value.
    QR_ALLOWED_LIFETIME_SECONDS: tuple[int, ...] = (5, 10, 15, 20, 30, 60)

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
