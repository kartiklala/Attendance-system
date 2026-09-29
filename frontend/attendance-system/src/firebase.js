// Firebase is used ONLY for Google Authentication (client sign-in flow).
// Firestore is accessed exclusively through the FastAPI backend — never here.
import { initializeApp } from "firebase/app";
import {
  browserLocalPersistence,
  browserPopupRedirectResolver,
  getAuth,
  initializeAuth,
} from "firebase/auth";

const firebaseConfig = {
  apiKey: "AIzaSyBfbsCDXo0WOgzWTfQRISr1XjSpfT4K-m8",
  authDomain: "attendance-roaster-7ce62.firebaseapp.com",
  projectId: "attendance-roaster-7ce62",
  storageBucket: "attendance-roaster-7ce62.firebasestorage.app",
  messagingSenderId: "369935436911",
  appId: "1:369935436911:web:47f141a3c89c8e22e3519a",
  measurementId: "G-ZHNNHQS013",
};

const app = initializeApp(firebaseConfig);

// Firebase Authentication (Google sign-in only).
//
// Auth is initialised EXPLICITLY with the browser redirect resolver and local
// (IndexedDB) persistence instead of relying on getAuth()'s auto-detected
// defaults. signInWithRedirect tears the page down and reloads it, so two
// things MUST be configured up front or the returning app cannot recover the
// user and reports "signed out" even after a successful Google login:
//   1. browserPopupRedirectResolver — consumes the credential Google hands
//      back to the /__/auth/handler and delivers it to getRedirectResult().
//   2. browserLocalPersistence — stores the session so it survives the full
//      page reload the redirect causes (sessionStorage alone is not enough).
//
// initializeAuth throws if this app's Auth was already created (e.g. a hot
// module reload in dev); getAuth then returns that same existing instance, so
// the fallback keeps signInWithRedirect/getRedirectResult working.
let auth;
try {
  auth = initializeAuth(app, {
    popupRedirectResolver: browserPopupRedirectResolver,
    persistence: browserLocalPersistence,
  });
} catch {
  auth = getAuth(app);
}

export { auth };
export default app;
