"use client";

import React, { useEffect, useState, useSyncExternalStore } from "react";
import { useRouter } from "next/navigation";
import { useAuth } from "../lib/auth";
import { hasSeenLaunch, markLaunchSeen } from "../lib/launchSeen";
import styles from "./splash.module.css";

function subscribeReducedMotion(onChange: () => void): () => void {
  if (typeof window === "undefined" || !window.matchMedia) return () => {};
  const mq = window.matchMedia("(prefers-reduced-motion: reduce)");
  mq.addEventListener("change", onChange);
  return () => mq.removeEventListener("change", onChange);
}
function getReducedMotionSnapshot(): boolean {
  if (typeof window === "undefined" || !window.matchMedia) return false;
  return window.matchMedia("(prefers-reduced-motion: reduce)").matches;
}
function getReducedMotionServerSnapshot(): boolean {
  return false;
}

export default function WelcomePage() {
  const router = useRouter();
  const { status } = useAuth();
  
  const reducedMotion = useSyncExternalStore(
    subscribeReducedMotion,
    getReducedMotionSnapshot,
    getReducedMotionServerSnapshot,
  );
  
  const [ready, setReady] = useState(false);

  useEffect(() => {
    if (status === "authenticated") {
      router.replace("/");
      return;
    }
    if (hasSeenLaunch()) {
      router.replace("/login");
      return;
    }
    // eslint-disable-next-line react-hooks/set-state-in-effect
    setReady(true);
  }, [status, router]);

  useEffect(() => {
    if (!ready) return;
    
    // Netflix-style intro timing:
    // Animation in CSS takes 1.6s.
    // At 1.6s, the logo is fully faded out and scaled.
    // We then immediately route to login so the form appears.
    const duration = reducedMotion ? 600 : 1600;
    
    const timer = setTimeout(() => {
      markLaunchSeen();
      router.replace("/login");
    }, duration);

    return () => clearTimeout(timer);
  }, [ready, reducedMotion, router]);

  if (!ready) {
    return <div className={styles.page} />;
  }

  return (
    <div className={`${styles.page} ${reducedMotion ? styles.reduced : ""}`}>
      <div className={styles.logoContainer} aria-hidden="true">
        {/* Official MONOA "Mo" mark (Figma 226:196) */}
        <svg className={styles.mark} viewBox="0 0 46 46" fill="none">
          <circle cx="23" cy="23" r="23" fill="#1b2130" />
          <rect x="15" y="16" width="4" height="12" rx="2" fill="#99d9e0" />
          <rect x="27" y="16" width="4" height="12" rx="2" fill="#99d9e0" />
        </svg>
      </div>
      <h1 className="sr-only">MONOA</h1>
    </div>
  );
}
