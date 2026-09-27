// Firebase is used ONLY for Google Authentication (client sign-in flow).
// Firestore is accessed exclusively through the FastAPI backend — never here.
import { initializeApp } from "firebase/app";
import { getAuth } from "firebase/auth";

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
export const auth = getAuth(app);
export default app;
