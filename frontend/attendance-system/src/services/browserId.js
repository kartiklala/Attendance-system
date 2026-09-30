// A random identifier for THIS BROWSER PROFILE — not a device, not hardware.
//
// The web platform deliberately exposes no serial number, MAC address, IMEI or
// Android ID to a page, so nothing here could be a real "device id", and calling
// it one would be a lie. It is a UUID we mint once and persist, which is exactly
// the quantity shared-browser detection needs: one student handing a phone down a
// list keeps ONE id across many Google accounts, while thirty identical phones on
// one campus network get thirty distinct ids.
//
// It is never used to identify a person, never sent as a raw identifier for
// storage (the backend hashes it per session with a server secret), and never
// derived from canvas/audio/screen fingerprinting — an identical fleet of phones
// produces one fingerprint, which is the collision this replaces.
//
// Persistence is redundant on purpose (localStorage + IndexedDB): clearing one
// store is repaired from the other. iOS can still purge both after days of
// non-use; that only ever yields an UNKNOWN id, and the backend treats unknown as
// "no evidence" rather than as a violation. Nothing here blocks attendance.

const STORAGE_KEY = "attendify.browser_id";
const DB_NAME = "attendify_meta";
const STORE = "ids";
// Never hold up a student's scan or submission on a storage read: browsers can
// stall IndexedDB (private mode, quota, a blocking transaction). On a timeout we
// send an empty id, which is handled as "unknown" upstream.
const STORAGE_TIMEOUT_MS = 700;

const UUID_RE =
  /^[0-9a-f]{8}-[0-9a-f]{4}-[0-9a-f]{4}-[0-9a-f]{4}-[0-9a-f]{12}$/i;

let cachedId = "";

function isValidId(value) {
  return typeof value === "string" && UUID_RE.test(value);
}

function mintId() {
  // crypto.randomUUID requires a secure context; both supported deployments
  // (Firebase Hosting over HTTPS and localhost) have one. The fallback keeps a
  // non-secure origin working rather than silently sending nothing.
  if (typeof crypto !== "undefined" && typeof crypto.randomUUID === "function") {
    return crypto.randomUUID();
  }
  const bytes = new Uint8Array(16);
  if (typeof crypto !== "undefined" && crypto.getRandomValues) {
    crypto.getRandomValues(bytes);
  } else {
    for (let i = 0; i < 16; i += 1) bytes[i] = Math.floor(Math.random() * 256);
  }
  bytes[6] = (bytes[6] & 0x0f) | 0x40; // version 4
  bytes[8] = (bytes[8] & 0x3f) | 0x80; // variant 10
  const hex = [...bytes].map((b) => b.toString(16).padStart(2, "0")).join("");
  return `${hex.slice(0, 8)}-${hex.slice(8, 12)}-${hex.slice(12, 16)}-${hex.slice(16, 20)}-${hex.slice(20)}`;
}

function readLocal() {
  try {
    const value = window.localStorage.getItem(STORAGE_KEY);
    return isValidId(value) ? value : "";
  } catch {
    return ""; // private mode / storage disabled / blocked by policy
  }
}

function writeLocal(value) {
  try {
    window.localStorage.setItem(STORAGE_KEY, value);
  } catch {
    /* non-fatal: IndexedDB still carries the id */
  }
}

function openMetaDb() {
  return new Promise((resolve, reject) => {
    if (typeof indexedDB === "undefined") {
      reject(new Error("indexedDB unavailable"));
      return;
    }
    let request;
    try {
      request = indexedDB.open(DB_NAME, 1);
    } catch (err) {
      reject(err);
      return;
    }
    request.onupgradeneeded = () => {
      const db = request.result;
      if (!db.objectStoreNames.contains(STORE)) db.createObjectStore(STORE);
    };
    request.onsuccess = () => resolve(request.result);
    request.onerror = () => reject(request.error || new Error("idb open failed"));
    request.onblocked = () => reject(new Error("idb open blocked"));
  });
}

async function idbRead() {
  const db = await openMetaDb();
  return new Promise((resolve, reject) => {
    const tx = db.transaction(STORE, "readonly");
    const get = tx.objectStore(STORE).get(STORAGE_KEY);
    get.onsuccess = () => resolve(isValidId(get.result) ? get.result : "");
    get.onerror = () => reject(get.error || new Error("idb get failed"));
    tx.onabort = () => reject(tx.error || new Error("idb read aborted"));
  });
}

async function idbWrite(value) {
  const db = await openMetaDb();
  return new Promise((resolve, reject) => {
    const tx = db.transaction(STORE, "readwrite");
    tx.objectStore(STORE).put(value, STORAGE_KEY);
    tx.oncomplete = () => resolve(true);
    tx.onerror = () => reject(tx.error || new Error("idb put failed"));
    tx.onabort = () => reject(tx.error || new Error("idb write aborted"));
  });
}

// IndexedDB must never be able to stall the attendance flow.
function withTimeout(promise) {
  return Promise.race([
    promise,
    new Promise((resolve) => {
      setTimeout(() => resolve(""), STORAGE_TIMEOUT_MS);
    }),
  ]);
}

let inFlight = null;

async function resolveId() {
  // 1. Fast path: the mirrored localStorage value.
  let value = readLocal();
  if (value) {
    cachedId = value;
    return value;
  }
  // 2. localStorage was cleared or blocked — the IndexedDB mirror restores the
  //    SAME id, so a student is not reborn as a new browser by a purge.
  value = await withTimeout(idbRead());
  if (value) {
    writeLocal(value);
    cachedId = value;
    return value;
  }
  // 3. Genuinely first run: mint once, then persist to both stores.
  value = mintId();
  writeLocal(value);
  withTimeout(idbWrite(value)); // fire-and-forget; localStorage already holds it
  cachedId = value;
  return value;
}

/**
 * This browser's persistent id. Resolves to "" if storage is completely
 * unavailable: callers must treat that as "unknown", never as an error, and the
 * backend treats it as the absence of evidence.
 */
export async function getBrowserId() {
  if (cachedId) return cachedId;
  // Collapse concurrent callers (StrictMode double-invoke, parallel submits) into
  // one mint so two ids can never be created for one profile.
  if (!inFlight) {
    inFlight = resolveId().finally(() => {
      inFlight = null;
    });
  }
  return inFlight;
}

/** Last known id, synchronously ("" before the first resolve). */
export function getCachedBrowserId() {
  return cachedId;
}
