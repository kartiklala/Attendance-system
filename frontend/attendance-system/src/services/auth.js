// Firebase Authentication helpers (Google sign-in is a client flow;
// all authorization decisions happen in FastAPI).
import {
  GoogleAuthProvider,
  getRedirectResult,
  onAuthStateChanged,
  signInWithRedirect,
  signOut,
} from "firebase/auth";
import { auth } from "../firebase";

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

export function startGoogleSignIn() {
  try {
    if (window.location.pathname === "/attendance" && window.location.search) {
      localStorage.setItem(
        RETURN_URL_KEY,
        JSON.stringify({ url: window.location.href, at: Date.now() })
      );
    }
  } catch {
    /* private mode etc. — worst case the student rescans the QR */
  }
  const provider = new GoogleAuthProvider();
  provider.setCustomParameters({ prompt: "select_account" });
  return signInWithRedirect(auth, provider);
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

// Called once on app boot: resolves with the credential when the app is
// reloaded after the redirect. onAuthStateChanged also fires if the result
// was already consumed by the SDK in this browser.
export async function completeRedirectSignIn() {
  try {
    return await getRedirectResult(auth);
  } catch (err) {
    if (err?.code === "auth/popup-closed-by-user") return null;
    throw err;
  }
}

export function signOutUser() {
  return signOut(auth);
}

export function observeAuthUser(callback) {
  return onAuthStateChanged(auth, callback);
}

// Force a refresh so /authorize-user always receives a valid ID token.
export async function getFirebaseIdToken(forceRefresh = true) {
  const user = auth.currentUser;
  if (!user) return null;
  return user.getIdToken(forceRefresh);
}

export function getCurrentFirebaseUser() {
  return auth.currentUser;
}
