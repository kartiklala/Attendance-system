I need you to investigate and FIX a recurring Google Sign-In issue in my application.

## The exact problem

The Google OAuth flow itself appears to work:

1. User opens the application.
2. User clicks **Sign in with Google**.
3. Google account selector opens.
4. User selects their Google account.
5. Google authentication completes.
6. User is redirected back to my application.
7. The application checks the authentication state.
8. **The application says the user is signed out and shows the Google Sign-In button again.**

This happens consistently.

## IMPORTANT: Existing console evidence

There are already debug logs in the application. After completing Google Sign-In and returning to the application, these are the actual logs:

```text
[AUTH] initialization started {returnedFromRedirect: true}
[AUTH] initialization started {returnedFromRedirect: true}

[AUTH] auth state deferred until redirect completes {hasUser: false}

[AUTH] redirect result checked {
  pathname: '/',
  returnedFromRedirect: true,
  resolvedUser: false
}

[AUTH] redirect result checked {
  pathname: '/',
  returnedFromRedirect: true,
  resolvedUser: false
}

[AUTH] auth state: signed out
```

These logs are extremely important.

Do NOT ignore them and do NOT simply add another workaround.

---

# What I want you to investigate

Trace exactly why this happens:

```text
Google authentication
        ↓
redirect back to application
        ↓
returnedFromRedirect = true
        ↓
auth state is deferred
        ↓
getRedirectResult() reports resolvedUser = false
        ↓
auth state becomes "signed out"
```

The key question is:

> **Why does Firebase/application have no resolved user immediately after the Google redirect, and why is the application treating that result as definitively signed out?**

Determine whether the problem is:

1. Firebase's redirect result handling
2. Firebase Auth initialization
3. Firebase persistence
4. Multiple Firebase Auth instances
5. Incorrect `authDomain`
6. Incorrect Google OAuth configuration
7. Redirect handling
8. A race condition between `onAuthStateChanged()` and `getRedirectResult()`
9. The application incorrectly suppressing the real auth-state callback
10. Some code explicitly calling `signOut()`
11. The application incorrectly interpreting an initial `null` user
12. Something else

Do not assume which one it is.

---

# VERY IMPORTANT: investigate the current implementation first

Before changing anything, inspect the actual current code.

Search the entire project for:

```text
initializeApp
initializeAuth
getAuth
getRedirectResult
signInWithRedirect
signInWithPopup
onAuthStateChanged
browserLocalPersistence
browserSessionPersistence
setPersistence
GoogleAuthProvider
signOut
returnedFromRedirect
redirectUserRef
auth state
```

Especially inspect:

```text
src/firebase.js
src/context/AuthContext.jsx
src/pages/StudentAttendance.jsx
```

but do NOT limit the investigation to those files.

Look for duplicate Firebase initialization and multiple Auth instances.

---

# Analyze the existing logs correctly

The current logs indicate:

```text
returnedFromRedirect: true
```

So the application correctly knows that it has returned from a Google redirect.

Then:

```text
auth state deferred until redirect completes {hasUser: false}
```

This means the application's own logic is intentionally preventing the normal auth-state callback from immediately deciding the user is signed out.

Then:

```text
redirect result checked ... resolvedUser: false
```

This is the critical point.

Then:

```text
auth state: signed out
```

The application is ultimately committing to the signed-out state.

I want you to determine whether:

```js
getRedirectResult(auth)
```

is actually returning `null`, and if so, **why**.

At the same time, determine what:

```js
auth.currentUser
```

contains immediately before and after `getRedirectResult()`.

Also determine exactly what `onAuthStateChanged()` emits during this period.

---

# Add better diagnostic logging if necessary

If the current logs aren't enough, temporarily add detailed logging around the entire sequence.

For example, log:

```text
[AUTH DEBUG] Firebase auth instance created
[AUTH DEBUG] auth.currentUser before redirect result
[AUTH DEBUG] getRedirectResult started
[AUTH DEBUG] getRedirectResult completed
[AUTH DEBUG] redirect result user
[AUTH DEBUG] auth.currentUser after redirect result
[AUTH DEBUG] onAuthStateChanged fired
[AUTH DEBUG] observer user
[AUTH DEBUG] final auth decision
[AUTH DEBUG] signOut called
```

For a user object, log only safe identifying information such as:

```text
uid
email
providerId
```

Never log:

* access tokens
* refresh tokens
* passwords
* credentials

Also log Firebase error codes/messages if any operation throws.

---

# Investigate the redirect lifecycle

I want you to determine whether this is actually a successful Firebase redirect authentication or whether Google/Firebase is returning to the application without completing the Firebase credential exchange.

Trace:

```text
signInWithRedirect()
        ↓
Google
        ↓
Firebase /__/auth/handler
        ↓
application reload
        ↓
Firebase Auth initialization
        ↓
getRedirectResult()
        ↓
onAuthStateChanged()
```

Determine which step fails.

Do not simply assume that because the Google account-selection page appeared, Firebase successfully established the authenticated Firebase session.

---

# Investigate Firebase persistence

Check exactly how Firebase Auth persistence is configured.

Determine:

* Which Firebase Auth instance is being used.
* Which persistence mechanism is active.
* Whether `browserLocalPersistence` is actually applied.
* Whether it is applied to the same Auth instance used by `signInWithRedirect()`.
* Whether `initializeAuth()` and `getAuth()` are accidentally creating/using different instances.
* Whether persistence initialization occurs before authentication.
* Whether anything clears IndexedDB/local storage.
* Whether anything calls `signOut()` during initialization.

Do not blindly change persistence.

First establish what is currently happening.

---

# Investigate the possibility of a race condition

The current logs strongly suggest that there may be a timing/state-machine issue.

Investigate whether the application is doing something conceptually like:

```text
Firebase starts
        ↓
onAuthStateChanged(null)
        ↓
application suppresses it because redirect is pending
        ↓
getRedirectResult() returns null
        ↓
application assumes signed out
        ↓
real Firebase auth state arrives later
        ↓
but application has already committed to signed out
```

If this is occurring, prove it with logs.

Also determine whether the duplicate:

```text
[AUTH] initialization started
```

and duplicate:

```text
[AUTH] redirect result checked
```

indicate that the AuthContext/provider is being initialized more than once.

Investigate why those logs appear twice.

Do NOT simply deduplicate the logs. Determine whether there are actually two instances/effects/providers executing.

---

# Check whether getRedirectResult() is being used incorrectly

Do not assume:

```js
getRedirectResult(auth) === null
```

means:

```text
Firebase user is signed out.
```

Those are not necessarily equivalent.

The authoritative application state should be based on the actual Firebase Auth state.

Determine the correct architecture for handling:

```text
initial auth loading
authenticated
unauthenticated
redirect processing
redirect error
```

The application should not prematurely render "signed out" while Firebase is still restoring authentication.

---

# Check Firebase and Google configuration

Only if the code investigation points toward configuration, verify:

### Firebase

* `projectId`
* `authDomain`
* `appId`
* Google provider enabled
* Authorized domains

### Google OAuth

* OAuth client
* Firebase redirect handler
* authorized redirect URI
* correct Firebase project

The deployed application is:

```text
attendance-roaster-7ce62.web.app
```

and the Firebase auth domain is expected to correspond to:

```text
attendance-roaster-7ce62.firebaseapp.com
```

Do not tell me to modify Firebase Console settings unless the investigation provides evidence that configuration is responsible.

---

# Check for accidental sign-out

Search the entire application for:

```js
signOut(
```

Determine whether `signOut()` is being called anywhere during:

* initial page load
* auth initialization
* redirect handling
* route changes
* component cleanup
* attendance initialization
* error handling

If it is being called, identify exactly why.

---

# Check routing

The redirect appears to return to:

```text
/
```

according to:

```text
pathname: '/'
```

Investigate whether this is expected.

Check whether:

* the attendance route is lost
* query parameters are lost
* the attendance token is lost
* the application redirects to `/`
* `/` initializes a separate auth flow
* the root page mounts another AuthContext
* the root page causes the authentication state to reset

Do not change routing unless necessary.

---

# Do not stack more workarounds

Previous attempts have already tried:

* `redirectUserRef`
* deferred auth state
* `browserPopupRedirectResolver`
* `browserLocalPersistence`
* additional AuthContext logic
* allowing `StudentAttendance.advance()` to recover

These did not solve the underlying issue.

Therefore:

**Do not add another timeout, retry loop, arbitrary delay, polling mechanism, or additional state flag just to make the UI appear signed in.**

I want the root cause fixed.

If previous changes are now unnecessary or are actually contributing to the bug, clean them up.

---

# Required architecture after the fix

The authentication flow should have a clear state model:

```text
INITIALIZING
      ↓
FIREBASE RESTORING SESSION
      ↓
 ┌────┴────┐
 ↓         ↓
USER      NULL
 ↓         ↓
SIGNED IN  SIGNED OUT
```

While Firebase is still determining the persisted session:

```text
Do NOT show "signed out".
```

Only after Firebase has definitively completed initialization should the application transition to:

```text
SIGNED IN
```

or:

```text
SIGNED OUT
```

For Google redirect handling, make sure the redirect result and Firebase Auth observer are coordinated correctly without one incorrectly overwriting the other.

---

# Important testing requirements

After making the fix:

```text
npm run lint
npm run build
```

Then perform a real browser test if the environment allows it.

Test exactly:

```text
1. Open a fresh Incognito window.
2. Open the deployed application.
3. Start the attendance flow.
4. Click "Sign in with Google".
5. Select a Google account.
6. Complete authentication.
7. Return to the application.
8. Check the browser console.
9. Confirm the application shows the authenticated user.
10. Refresh the page.
11. Confirm the user remains authenticated.
12. Start another attendance action if applicable.
13. Log out.
14. Confirm the application becomes signed out.
15. Sign in again.
16. Confirm authentication works again.
```

Also test the same flow locally if appropriate.

---

# What I expect in your final response

Do not simply say:

> "Fixed."

Give me:

### 1. Root cause

Exactly what caused the authenticated Firebase user to disappear/not be recognized after the Google redirect.

### 2. Evidence

Explain what the existing logs showed and what additional evidence you found.

In particular, explain these lines:

```text
auth state deferred until redirect completes {hasUser: false}

redirect result checked {
  pathname: '/',
  returnedFromRedirect: true,
  resolvedUser: false
}

auth state: signed out
```

### 3. Why the previous fixes didn't work

Explain why the earlier changes to `AuthContext`, persistence, and redirect handling did not resolve the underlying problem.

### 4. Files changed

List every modified file and explain the change.

### 5. Authentication flow after the fix

Show the corrected flow:

```text
Google
 ↓
Firebase redirect
 ↓
Firebase Auth restoration
 ↓
Auth observer / redirect result
 ↓
AuthContext
 ↓
authenticated application
```

### 6. Verification

Report:

```text
npm run lint
npm run build
```

and whether an actual Google OAuth round trip was successfully tested.

### 7. If it still cannot be fixed

If you reach a point where the code is demonstrably correct but Firebase/Google configuration is the blocker, **do not make up a solution**.

Tell me exactly:

```text
What was verified
What failed
What evidence proves it
What exact Firebase/Google Console setting needs checking
```

The priority is:

> **Find the actual point where the authenticated Google/Firebase session is lost after the redirect, fix that root cause, and prove the fix rather than adding another workaround.**
