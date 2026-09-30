// Firebase Authentication helpers (Google sign-in is a client flow;
// all authorization decisions happen in FastAPI).
import {
  GoogleAuthProvider,
  getRedirectResult,
  onAuthStateChanged,
  signInWithPopup,
  signInWithRedirect,
  signOut,
} from "firebase/auth";
import { auth } from "../firebase";
import { authLog } from "../utils/authLog";

// Full-page redirect instead of a popup: works even when the browser
// blocks pop-ups. The page reloads after sign-in and the pending result is
// consumed by completeRedirectSignIn().
//
// Google always returns to the app ROOT, which would drop the QR token from
// /attendance?token=… and trap students in a login loop. So the exact URL
// is parked (10-minute validity) right before the redirect and restored by
// AuthContext when the app reloads.
const RETURN_URL_KEY = "pending_attendance_return";
const RETURN_URL_TTL_MS = 10 * 60 * 1000;
// The individual attendance attempt survives the full-page Google redirect in
// sessionStorage (tab-scoped, cleared on tab close). Only the opaque attempt id
// plus the server-reported remaining time and a local stamp for the display
// countdown are stored — never the application JWT, a Firebase token, or any
// student identity data.
const ATTEMPT_KEY = "attendance_attempt";

// The redirect loop guard that actually survives the redirect. signInWithRedirect()
// tears the page down, so an in-memory ref is wiped on the way back to Google:
// the only thing that outlives the full page load is sessionStorage (which is
// also what carries the attendance attempt). We mark the redirect as *left*
// before leaving, and the returning page must consume that marker instead of
// assuming a fresh automatic sign-in is allowed.
const REDIRECT_LEFT_KEY = "google_redirect_in_flight";

export function markGoogleRedirectLeft() {
  try {
    sessionStorage.setItem(REDIRECT_LEFT_KEY, "1");
  } catch {
    /* storage unavailable: the in-memory guard still applies to this page */
  }
}

// True (once) when this page load is the return from a redirect we started.
// Consuming it clears the marker, so a later genuine sign-in can be started.
export function consumeReturnFromGoogleRedirect() {
  try {
    const returned = sessionStorage.getItem(REDIRECT_LEFT_KEY) === "1";
    if (returned) sessionStorage.removeItem(REDIRECT_LEFT_KEY);
    return returned;
  } catch {
    return false;
  }
}

export function clearGoogleRedirectMarker() {
  try {
    sessionStorage.removeItem(REDIRECT_LEFT_KEY);
  } catch {
    /* nothing to clear */
  }
}

// ---- Platform-aware Google sign-in ----------------------------------------
//
// ONE Firebase Auth instance drives everything; this only chooses HOW to reach
// Google. DESKTOP keeps the reliable in-page popup (it works and needs no page
// reload — the AuthContext observer picks the user straight up). MOBILE uses
// signInWithRedirect because iOS Safari and in-app browsers silently drop or
// block the popup, leaving the app reporting signed-out. The redirect tears the
// page down, so the in-flight marker + the parked /attendance URL below let the
// app recover the user and resume the SAME attempt on the way back.
export function isMobileDevice() {
  if (typeof navigator === "undefined") return false;
  const ua = navigator.userAgent || "";
  // iPadOS 13+ Safari reports a desktop "Macintosh" UA but is touch-first, so
  // maxTouchPoints is the reliable signal there. The UA tokens otherwise cover
  // iPhone/iPad Safari and Android Chrome — one broad check, not one device.
  const isIpadOS = /Macintosh/.test(ua) && navigator.maxTouchPoints > 1;
  return /iPhone|iPad|iPod|Android|Mobile/i.test(ua) || isIpadOS;
}

function newGoogleProvider(selectAccount = false) {
  const provider = new GoogleAuthProvider();
  // Only ask Google for the account chooser when the user explicitly wants a
  // different account. Sending prompt=select_account on EVERY sign-in is what
  // made Google keep showing the picker even when an account was already
  // available, which read to users as "it asks me to sign in multiple times".
  if (selectAccount) {
    provider.setCustomParameters({ prompt: "select_account" });
  }
  return provider;
}

// Park the exact /attendance?token=… URL (or the plain resume path when an
// attempt is already running) so the redirect return can continue it. Best
// effort: a private-mode failure just means the student rescans the QR.
function parkAttendanceReturn() {
  let attemptPresent = false;
  try {
    if (window.location.pathname === "/attendance") {
      attemptPresent = Boolean(loadAttendanceAttempt());
      // With a live attempt the QR token has already proven entry: return to
      // the plain attendance page and resume the SAME attempt (no rescan, and
      // never restart the server-side window). Without one, park the full
      // token URL so the entry gateway can still run on the way back.
      const resume = attemptPresent
        ? `${window.location.origin}/attendance`
        : window.location.search
          ? window.location.href
          : null;
      if (resume) {
        localStorage.setItem(
          RETURN_URL_KEY,
          JSON.stringify({ url: resume, at: Date.now() })
        );
      }
    }
  } catch {
    /* ignore — worst case the student rescans the QR */
  }
  return attemptPresent;
}

// signInWithRedirect navigates away and the page reloads on Google's return,
// so this promise normally never resolves in the calling page.
async function runGoogleRedirect(selectAccount = false) {
  const attemptPresent = parkAttendanceReturn();
  // Mark BEFORE the call: navigation to Google can begin immediately, so a
  // later page lifetime can never observe an un-marked trip and mistake the
  // return for a fresh automatic sign-in (the redirect loop).
  markGoogleRedirectLeft();
  authLog("sign-in method selected: redirect", {
    pathname: window.location.pathname,
    hasAttempt: attemptPresent,
    selectAccount,
  });
  authLog("redirect sign-in started");
  return signInWithRedirect(auth, newGoogleProvider(selectAccount));
}

// The single entry point every screen uses (via AuthContext.login).
// `selectAccount` is opt-in: the default sign-in silently reuses whatever
// Google session already exists, which is the correct behaviour when the app
// is only restoring a session the user already has.
export async function signInWithGoogle({ selectAccount = false } = {}) {
  if (isMobileDevice()) {
    return runGoogleRedirect(selectAccount);
  }
  authLog("sign-in method selected: popup", { selectAccount });
  authLog("popup sign-in started");
  try {
    const result = await signInWithPopup(auth, newGoogleProvider(selectAccount));
    authLog("popup sign-in completed");
    return result;
  } catch (err) {
    // Only a GENUINELY blocked popup justifies escalating to a full-page
    // redirect. A popup the user closed, or a duplicate in-flight popup
    // request, must be surfaced instead (PART 9).
    if (err?.code === "auth/popup-blocked") {
      authLog("popup blocked; falling back to redirect");
      return runGoogleRedirect(selectAccount);
    }
    throw err;
  }
}

// Read-once + clear: a stale return URL must never fire at a later date.
export function takeAttendanceReturnUrl() {
  try {
    const raw = localStorage.getItem(RETURN_URL_KEY);
    if (!raw) return null;
    localStorage.removeItem(RETURN_URL_KEY);
    const { url, at } = JSON.parse(raw);
    if (!url || typeof url !== "string" || Date.now() - at > RETURN_URL_TTL_MS) {
      return null;
    }
    return url;
  } catch {
    return null;
  }
}

// getRedirectResult() is a ONE-TIME read: Firebase clears the stored redirect
// response as part of consuming it. React's <StrictMode> (dev) mounts every
// effect twice, so the bootstrap effect calls completeRedirectSignIn() twice.
// Without memoization the first call claims the response (and its result is
// thrown away when StrictMode immediately unmounts that effect run) while the
// second call finds nothing and resolves null — the credential is lost and the
// app correctly-but-tragically concludes "signed out". Caching the promise at
// module scope makes the whole function idempotent for the page lifetime: the
// redirect is finalized exactly once and every caller observes the SAME resolved
// UserCredential. This is not a retry/timeout/polling workaround — it aligns our
// usage with the one-time contract Firebase documents.
let redirectResultPromise = null;

// Only safe, non-secret user fields are ever logged (never a token).
function describeUser(user) {
  if (!user) return null;
  return { uid: user.uid, email: user.email || null, providerId: "google.com" };
}

// Called once on app boot: resolves with the credential when the app is
// reloaded after the redirect. onAuthStateChanged also fires once the SDK has
// applied the redirect credential to the persisted session.
//
// Redirect failures must NOT be swallowed: the caller needs to know whether
// this was even a return from a redirect, otherwise a rejected OAuth response
// looks identical to "student cancelled". Only safe diagnostics are reported —
// never a token.
export function completeRedirectSignIn() {
  if (redirectResultPromise) return redirectResultPromise;
  const returnedFromRedirect = isReturningFromGoogleRedirect();
  authLog("getRedirectResult started", {
    returnedFromRedirect,
    currentUserBefore: describeUser(getCurrentFirebaseUser()),
  });
  redirectResultPromise = getRedirectResult(auth)
    .then((result) => {
      authLog("redirect result checked", {
        pathname: window.location.pathname,
        returnedFromRedirect,
        resolvedUser: Boolean(result?.user),
        currentUserAfter: describeUser(getCurrentFirebaseUser()),
      });
      return result;
    })
    .catch((err) => {
      // A failed exchange must allow a fresh attempt on a later navigation, so
      // drop the cached promise here; a SUCCESSFUL read stays cached forever.
      redirectResultPromise = null;
      authLog("redirect result failed", {
        code: err?.code || "unknown",
        pathname: window.location.pathname,
        returnedFromRedirect,
        hasAttempt: Boolean(loadAttendanceAttempt()),
      });
      if (err?.code === "auth/popup-closed-by-user") return null;
      throw err;
    });
  return redirectResultPromise;
}

// Whether this page load began by returning from a redirect we started
// (peek only — does not consume the marker).
export function isReturningFromGoogleRedirect() {
  try {
    return sessionStorage.getItem(REDIRECT_LEFT_KEY) === "1";
  } catch {
    return false;
  }
}

export function signOutUser() {
  return signOut(auth);
}

export function observeAuthUser(callback) {
  return onAuthStateChanged(auth, callback);
}

// Force a refresh so /authorize-user always receives a valid ID token.
// `forceRefresh` defaults to FALSE: after the redirect return the SDK already
// holds a fresh ID token for the signed-in user, and an unnecessary forced
// refresh adds a network round trip to Google's token endpoint on the critical
// path. Genuine expiry is still handled by the api layer's 401 recovery, which
// passes true.
export async function getFirebaseIdToken(forceRefresh = false) {
  const user = auth.currentUser;
  if (!user) return null;
  return user.getIdToken(forceRefresh);
}

export function getCurrentFirebaseUser() {
  return auth.currentUser;
}

// ---- Attendance attempt persistence (survives the Google redirect) ---------

export function saveAttendanceAttempt(data) {
  const attempt = {
    id: data.attempt_id,
    // Server-authoritative window; `at` is only used to approximate the
    // countdown across the redirect. The backend re-checks expiry on submit.
    remaining: Number(data.remaining_seconds) || 0,
    at: Date.now(),
  };
  try {
    sessionStorage.setItem(ATTEMPT_KEY, JSON.stringify(attempt));
  } catch {
    /* storage unavailable: the flow still works inside this page lifetime */
  }
  return { attemptId: attempt.id, timeLeft: attempt.remaining };
}

// Returns the in-flight attempt (with the seconds left recomputed locally for
// display) or null when nothing is stored / it already ran out. Expiry is only
// ever ENFORCED by the backend; this just avoids offering a dead form.
export function loadAttendanceAttempt() {
  try {
    const raw = sessionStorage.getItem(ATTEMPT_KEY);
    if (!raw) return null;
    const { id, remaining, at } = JSON.parse(raw);
    if (!id || typeof remaining !== "number") return null;
    const elapsed = Math.max(0, Math.floor((Date.now() - (at || 0)) / 1000));
    const left = remaining - elapsed;
    if (left <= 0) {
      sessionStorage.removeItem(ATTEMPT_KEY);
      return null;
    }
    return { attemptId: id, timeLeft: left };
  } catch {
    return null;
  }
}

export function clearAttendanceAttempt() {
  try {
    sessionStorage.removeItem(ATTEMPT_KEY);
  } catch {
    /* nothing to clear */
  }
}
