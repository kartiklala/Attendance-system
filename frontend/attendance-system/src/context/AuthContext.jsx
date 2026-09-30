// AuthContext: Firebase sign-in state + backend authorization (role) state.
// The role always comes from FastAPI's /authorize-user response — the
// frontend never decides whether a user is a CR.
import { createContext, useCallback, useContext, useEffect, useRef, useState } from "react";
import * as api from "../services/api";
import {
  clearAttendanceAttempt,
  clearGoogleRedirectMarker,
  completeRedirectSignIn,
  getCurrentFirebaseUser,
  getFirebaseIdToken,
  isReturningFromGoogleRedirect,
  observeAuthUser,
  signInWithGoogle,
  signOutUser,
  takeAttendanceReturnUrl,
} from "../services/auth";
import { authLog } from "../utils/authLog";

const AuthContext = createContext(null);

export function AuthProvider({ children }) {
  const [firebaseUser, setFirebaseUser] = useState(undefined); // undefined = loading
  const [role, setRole] = useState(null); // null | "admin" | "cr" | "student"
  const [profile, setProfile] = useState(null); // { uid, name, email, role }
  const [error, setError] = useState(null);
  // Set when the backend permanently rejects the application JWT and it
  // could not be re-issued: all session/attendance UI state must be dropped
  // and fresh authorization required (spec: no stale "Continue").
  const [requiresReauth, setRequiresReauth] = useState(false);
  // Three distinct authentication states — `firebaseUser === null` alone means
  // "definitely signed out", never "not decided yet":
  //   authSettled=false → Firebase has not finished determining the user
  //   authSettled=true, firebaseUser=null → genuinely signed out
  //   authSettled=true, firebaseUser set  → signed in
  const [authSettled, setAuthSettled] = useState(false);
  const mounted = useRef(true);
  // True only for the brief window after a redirect return, while
  // getRedirectResult() is still being consumed. Auth callbacks that arrive
  // during this window are provisional and must not decide sign-in state.
  const redirectPendingRef = useRef(isReturningFromGoogleRedirect());
  // A real user observed while the redirect was pending. getRedirectResult()
  // can resolve null on the app root even after a successful sign-in, so this
  // is the fallback that keeps a late `settle` from declaring "signed out".
  const redirectUserRef = useRef(null);

  useEffect(() => {
    mounted.current = true;
    return () => {
      mounted.current = false;
    };
  }, []);

  const authorize = useCallback(async (idToken) => {
    const data = await api.authorizeUser(idToken);
    if (!mounted.current) return null;
    setProfile(data.user);
    setRole(data.user.role);
    setError(null);
    setRequiresReauth(false);
    return data.user;
  }, []);

  // Re-authorize with the current Firebase session (used by the api layer
  // when the 3-minute application JWT expires mid-session). If recovery is
  // impossible, the api layer calls the permanent handler: clear the
  // authenticated profile so every protected screen falls back to sign-in.
  useEffect(() => {
    api.setAuthRecovery(
      async () => {
        try {
          const token = await getFirebaseIdToken(true);
          if (!token) return false;
          await authorize(token);
          return true;
        } catch {
          return false;
        }
      },
      () => {
        if (!mounted.current) return;
        setProfile(null);
        setRole(null);
        setRequiresReauth(true);
        setError("Your session expired. Please sign in again.");
      }
    );
    return () => api.setAuthRecovery(null, null);
  }, [authorize]);

  // Restore a parked /attendance?token=… link only once the redirect
  // sign-in has FULLY completed. Gating on `role` (set solely by a successful
  // /authorize-user) means the HttpOnly cookie is committed BEFORE we
  // navigate — navigating on `firebaseUser` alone reloads the page mid-flight
  // and aborts that first /authorize-user, leaving no cookie (UNAUTHORIZED).
  //
  // If the parked URL is the route we are already on, do not "navigate" with a
  // full-page reload at all: an identical assign() reloads the app for nothing
  // and restarts this whole sequence.
  useEffect(() => {
    if (!role) return;                            // not authorized yet: wait
    if (window.location.pathname !== "/") return;  // only recover the root landing
    const returnUrl = takeAttendanceReturnUrl();
    if (!returnUrl) return;
    let target;
    try {
      target = new URL(returnUrl, window.location.origin);
    } catch {
      return; // unreadable parked value: drop it rather than loop
    }
    const sameRoute =
      target.pathname === window.location.pathname &&
      target.search === window.location.search;
    if (sameRoute) {
      authLog("already on the resumed route, skipping reload", {
        pathname: target.pathname,
      });
      return;
    }
    authLog("resuming parked route", { pathname: target.pathname });
    window.location.assign(returnUrl);
  }, [role]);

  // Decide the authentication state from the CURRENT user the observer reports.
  // Google sign-in resolves IN-PAGE via a popup (no full reload), so a real
  // user can legitimately appear AFTER the app first rendered a signed-out
  // state. There is no redirect return whose provisional `null` could race the
  // credential, so we must NOT latch the first decision: doing so made a later
  // popup user only refresh `firebaseUser` and never re-run /authorize-user,
  // leaving `role` null and the UI stuck on "Authorizing…". Every observed
  // state is therefore decided on its own merit — a user always authorizes, an
  // absent user always clears the role.
  const decide = useCallback(
    async (user) => {
      authLog(user ? "auth state: signed in" : "auth state: signed out");
      setFirebaseUser(user || null);
      setAuthSettled(true);
      if (!user) {
        setRole(null);
        setProfile(null);
        setRequiresReauth(false);
        return;
      }
      // A completed redirect that yields an account ends the one-time guard.
      clearGoogleRedirectMarker();
      // Timing the two awaited round trips (Firebase ID token + backend
      // /authorize-user) is what separates a slow cold Render backend from a
      // stalled token refresh when users report a long "Authorizing…" pause.
      const startedAt = Date.now();
      try {
        const token = await user.getIdToken(false);
        await authorize(token);
        authLog("application session established", {
          durationMs: Date.now() - startedAt,
        });
      } catch (err) {
        authLog("authorization failed", {
          durationMs: Date.now() - startedAt,
          code: err?.code || "unknown",
        });
        if (mounted.current) {
          setError(err?.message || "Authorization failed.");
        }
      }
    },
    [authorize]
  );

  // Bootstrap: consume any pending redirect result BEFORE deciding. Google's
  // redirect forces a full reload, and while getRedirectResult() resolves the
  // SDK can briefly report onAuthStateChanged(null). Treating that provisional
  // null as "signed out" re-triggered signInWithRedirect() and looped forever.
  //
  // The marker is PEEKED (not consumed) here so StudentAttendance can later
  // detect "we already sent this student to Google" and offer a manual button
  // instead of auto-redirecting again.
  useEffect(() => {
    const returned = isReturningFromGoogleRedirect();
    authLog("initialization started", { returnedFromRedirect: returned });
    let cancelled = false;
    const settle = (result, failure) => {
      if (cancelled) return;
      if (failure) {
        authLog("redirect result rejected", { code: failure.code || "unknown" });
      }
      redirectPendingRef.current = false;
      // Prefer the settled user: the SDK's own currentUser, then any real user
      // the observer already reported during the pending window, then the
      // redirect result. A null here only happens if genuinely nobody signed in.
      //
      // Logging WHICH source won is the iOS diagnostic: on Safari the redirect
      // result frequently resolves null while currentUser is already populated,
      // and only a staging build with VITE_AUTH_DEBUG=true can confirm whether
      // the platform returns currentUser, getRedirectResult, or neither.
      const settledUser =
        getCurrentFirebaseUser() || redirectUserRef.current || result?.user || null;
      authLog("settling auth state", {
        fromCurrentUser: Boolean(getCurrentFirebaseUser()),
        fromObserver: Boolean(redirectUserRef.current),
        fromRedirectResult: Boolean(result?.user),
        settledSignedIn: Boolean(settledUser),
      });
      decide(settledUser);
    };
    if (returned) {
      redirectPendingRef.current = true;
      completeRedirectSignIn().then(
        (result) => settle(result, null),
        (err) => {
          if (mounted.current) {
            // Surface the real Firebase error code, not just a generic message:
            // auth/unauthorized-domain, auth/operation-not-supported-in-environment,
            // auth/internal-error, auth/network-request-failed etc. point at a
            // Firebase Console / configuration problem, not a code bug. The code
            // itself is not a token and is safe to show.
            setError(
              err?.code
                ? `Sign-in failed (${err.code}).`
                : err?.message || "Sign-in failed. Please try again."
            );
          }
          settle(null, err);
        }
      );
      // Safety net: never strand the student on a spinner if the SDK stalls.
      const watchdog = setTimeout(() => {
        if (redirectPendingRef.current) {
          authLog("redirect result watchdog fired");
          settle(null, null);
        }
      }, 10_000);
      return () => {
        cancelled = true;
        clearTimeout(watchdog);
      };
    }
    // No pending redirect: the observer decides normally. Still run
    // getRedirectResult once (documented pattern) but never gate on its result.
    completeRedirectSignIn().catch(() => {});
    return () => {
      cancelled = true;
    };
  }, [decide]);

  // Observer: publish auth state. The FIRST callback carrying a real user is
  // authoritative — on the app root, getRedirectResult() frequently resolves
  // with null even after a SUCCESSFUL redirect (the SDK has already applied
  // the credential to storage and delivered it here). So a user seen while the
  // redirect is pending settles signed-in immediately, while a provisional
  // null is simply deferred (never allowed to decide "signed out" too early,
  // which is what re-triggered the redirect loop).
  useEffect(() => {
    const unsubscribe = observeAuthUser((user) => {
      if (redirectPendingRef.current) {
        if (user) {
          authLog("redirect restored user via observer; settling signed-in");
          redirectUserRef.current = user;
          redirectPendingRef.current = false;
          decide(user);
          return;
        }
        authLog("auth state deferred until redirect completes", {
          hasUser: false,
        });
        return;
      }
      decide(user);
    });
    return unsubscribe;
  }, [decide]);

  // The single Google entry point. signInWithGoogle picks the mechanism by
  // platform: DESKTOP popup (resolves IN-PAGE — the observer then fires with
  // the user and `decide` authorizes them, no reload) and MOBILE redirect
  // (navigates away; the bootstrap effect above consumes the return on reload).
  // A blocked desktop popup transparently falls back to redirect inside
  // signInWithGoogle; a popup the user merely closed is reported, not chased.
  //
  // `options.selectAccount` is opt-in only: it asks Google for the account
  // chooser. Leaving it off lets an already-signed-in Google account complete
  // silently, which is what we want for a routine sign-in.
  const login = useCallback(async (options = {}) => {
    setError(null);
    try {
      await signInWithGoogle(options);
    } catch (err) {
      const cancelled =
        err?.code === "auth/popup-closed-by-user" ||
        err?.code === "auth/cancelled-popup-request";
      setError(
        cancelled
          ? "Sign-in was cancelled."
          : err?.message || "Sign-in failed. Please try again."
      );
      throw err;
    }
  }, []);

  // Guarantee a working backend application session for the CURRENT Firebase
  // user before any protected call. The redirect flow authorizes
  // asynchronously at boot, so the student flow awaits this instead of racing
  // an ambient /authorize-user (which caused 401 "not signed in" errors).
  //
  // When the session is ALREADY valid this returns immediately: re-running
  // getIdToken(true) + /authorize-user just because a button was pressed cost
  // two extra round trips and granted nothing. Genuinely expired/revoked
  // sessions still re-authorize (missing role, JWT about to expire, or the
  // api layer's 401 recovery path).
  const ensureSession = useCallback(async () => {
    if (role && !requiresReauth && api.hasValidApplicationToken()) return role;
    try {
      // Use the cached ID token (the boot listener already validated the
      // Firebase session); only the 401 recovery path forces a refresh.
      const token = await getFirebaseIdToken(false);
      if (!token) return null;
      const user = await authorize(token);
      return user ? user.role : null;
    } catch {
      return null;
    }
  }, [authorize, role, requiresReauth]);

  const logout = useCallback(async () => {
    try {
      await api.logout();
    } catch {
      /* cookie already gone or network issue — continue signing out locally */
    }
    // A signed-out student must never leave an attempt parked for whoever
    // signs in next in this tab (attempts are bound server-side anyway).
    clearAttendanceAttempt();
    clearGoogleRedirectMarker();
    // A signed-out observer resets role/state; a later popup user re-authorizes
    // through the same decide path, so no latch needs clearing here anymore.
    redirectPendingRef.current = false;
    redirectUserRef.current = null;
    setAuthSettled(false);
    setFirebaseUser(undefined);
    await signOutUser();
    setRole(null);
    setProfile(null);
    setRequiresReauth(false);
  }, []);

  return (
    <AuthContext.Provider
      value={{
        firebaseUser,   // Firebase user object or null (undefined while loading)
        authSettled,    // Firebase has definitively resolved signed-in/out
        profile,        // { uid, name, email, role } from the backend
        role,           // "admin" | "cr" | "student" | null
        error,
        requiresReauth, // JWT rejected and could not be re-issued
        login,
        logout,
        ensureSession,
      }}
    >
      {children}
    </AuthContext.Provider>
  );
}

export function useAuth() {
  const ctx = useContext(AuthContext);
  if (!ctx) throw new Error("useAuth must be used inside <AuthProvider>");
  return ctx;
}
