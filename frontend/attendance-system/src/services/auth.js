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
export function signInWithGoogle() {
  const provider = new GoogleAuthProvider();
  provider.setCustomParameters({ prompt: "select_account" });
  return signInWithRedirect(auth, provider);
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
