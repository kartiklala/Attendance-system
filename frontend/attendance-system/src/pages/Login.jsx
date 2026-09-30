// Login page: requests location on load, then Google sign-in.
// After authorization the backend's role decides where the user goes.
import { useEffect, useState } from "react";
import { useNavigate } from "react-router-dom";
import { useAuth } from "../context/AuthContext";
import { ErrorBox, GoogleIcon, BrandMark } from "../components/ui";
import { getCurrentLocation } from "../utils/geolocation";

export default function Login() {
  const { firebaseUser, authSettled, profile, role, error, login, logout } = useAuth();
  const navigate = useNavigate();
  const [locationState, setLocationState] = useState("prompting"); // prompting | granted | denied
  const [locationMessage, setLocationMessage] = useState("");
  const [loggingIn, setLoggingIn] = useState(false);
  // Escape hatch for the session-restore guard below: if Firebase never
  // settles (offline, bad API config, an SDK that silently fails to fire), the
  // UI must NOT stay on "Checking your sign-in…" with no way to sign in.
  const [restoreTimedOut, setRestoreTimedOut] = useState(false);

  // Step 1 on first website load: ask for browser location.
  useEffect(() => {
    let cancelled = false;
    getCurrentLocation()
      .then(() => !cancelled && setLocationState("granted"))
      .catch((err) => {
        if (cancelled) return;
        setLocationState("denied");
        setLocationMessage(err.message);
      });
    return () => {
      cancelled = true;
    };
  }, []);

  // Step 6: route by the role returned from POST /authorize-user.
  useEffect(() => {
    if (role === "cr") navigate("/cr", { replace: true });
    if (role === "admin") navigate("/admin", { replace: true });
  }, [role, navigate]);

  // Give up waiting on the restore check once it is clearly stuck. Generous
  // enough that a normal (or slow mobile) session restore settles first, so
  // the button never flashes early — but bounded, so nobody is stranded.
  useEffect(() => {
    if (firebaseUser !== undefined && authSettled) return;
    const timer = setTimeout(() => setRestoreTimedOut(true), 8000);
    return () => clearTimeout(timer);
  }, [firebaseUser, authSettled]);

  const handleLogin = async (selectAccount = false) => {
    setLoggingIn(true);
    try {
      await login({ selectAccount });
    } catch {
      /* error surfaced through context */
    } finally {
      setLoggingIn(false);
    }
  };

  const retryLocation = () => {
    setLocationState("prompting");
    getCurrentLocation()
      .then(() => setLocationState("granted"))
      .catch((err) => {
        setLocationState("denied");
        setLocationMessage(err.message);
      });
  };

  // firebaseUser === undefined  -> Firebase hasn't resolved the persisted session yet
  // authSettled === false        -> same thing, via the explicit settle flag
  // Only treat the user as "signed out" once both say the check is actually
  // done; otherwise a mobile/iOS user can see (and tap) the Google button
  // while an existing session is still being restored, forcing a second
  // sign-in on top of the one Firebase is already completing.
  const isResolvingSession =
    (firebaseUser === undefined || !authSettled) && !restoreTimedOut;

  return (
    <div className="page-center">
      <div className="card login-card">
        <div className="brand-lockup">
          <BrandMark size={64} />
          <div className="brand-text">
            <h1 className="app-title brand-name">Attendify</h1>
            <p className="brand-tag">Attendance Tracker &amp; Management</p>
          </div>
        </div>
        <p className="app-subtitle">Sign in with your Google account</p>

        <div className={`location-status ${locationState}`}>
          {locationState === "prompting" && "Requesting location access…"}
          {locationState === "granted" && "✓ Location access granted"}
          {locationState === "denied" && (
            <>
              <span>⚠ {locationMessage || "Location access is required."}</span>{" "}
              <button className="link-btn" onClick={retryLocation}>
                Retry
              </button>
            </>
          )}
        </div>

        {isResolvingSession ? (
          <p className="muted">Checking your sign-in…</p>
        ) : !firebaseUser ? (
          <button
            className="btn btn-primary btn-google"
            onClick={() => handleLogin(false)}
            disabled={loggingIn}
          >
            <GoogleIcon />
            {loggingIn ? "Redirecting to Google…" : "Sign in with Google"}
          </button>
        ) : role === "student" ? (
          <div className="student-wait">
            <p>
              You are signed in as <strong>{profile?.name || profile?.email}</strong>.
            </p>
            <p className="muted">
              To mark attendance, scan the QR code shown by your Class
              Representative.
            </p>
            <button className="btn btn-ghost btn-small" onClick={logout}>
              Sign out / use another account
            </button>
          </div>
        ) : (
          <p className="muted">Authorizing…</p>
        )}

        <ErrorBox message={error} />
      </div>
    </div>
  );
}
