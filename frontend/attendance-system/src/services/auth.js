// Firebase Authentication helpers (Google sign-in is a client flow;
// all authorization decisions happen in FastAPI).
import {
  GoogleAuthProvider,
  onAuthStateChanged,
  signInWithPopup,
  signOut,
} from "firebase/auth";
import { auth } from "../firebase";

export function signInWithGoogle() {
  const provider = new GoogleAuthProvider();
  provider.setCustomParameters({ prompt: "select_account" });
  return signInWithPopup(auth, provider);
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
