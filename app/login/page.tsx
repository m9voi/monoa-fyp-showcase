"use client";
import React, { Suspense, useEffect, useState } from "react";
import { useRouter, useSearchParams } from "next/navigation";
import { Lock, Mail, User, ArrowRight } from "lucide-react";
import { useAuth } from "../lib/auth";
import { ApiError } from "../lib/api";
import { safeInternalPath } from "../lib/safeRedirect";
import styles from "./login.module.css";

export default function AuthPage() {
  return (
    <Suspense fallback={<div className={styles.page} />}>
      <AuthPageInner />
    </Suspense>
  );
}

function AuthPageInner() {
  const [isLogin, setIsLogin] = useState(true);
  const [email, setEmail] = useState("");
  const [password, setPassword] = useState("");
  const [name, setName] = useState("");
  const [loading, setLoading] = useState(false);
  const [errorMsg, setErrorMsg] = useState("");

  const router = useRouter();
  const searchParams = useSearchParams();
  const { status, login, register } = useAuth();

  // Only ever redirect to a local path — never an attacker-supplied external URL.
  const nextPath = safeInternalPath(searchParams.get("next"));

  // Already signed in? Leave the auth screen.
  useEffect(() => {
    if (status === "authenticated") router.replace(nextPath);
  }, [status, nextPath, router]);

  const handleSubmit = async (e: React.FormEvent) => {
    e.preventDefault();
    if (loading) return;
    setErrorMsg("");
    setLoading(true);
    try {
      if (isLogin) {
        await login(email, password);
      } else {
        await register(email, password, name.trim() || undefined);
      }
      router.replace(nextPath);
    } catch (err) {
      if (err instanceof ApiError) {
        setErrorMsg(err.message);
      } else {
        setErrorMsg("Couldn't reach the server. Please try again.");
      }
      setLoading(false);
    }
  };

  return (
    <div className={styles.page}>
      <div className={styles.card}>
        <div className={styles.brand}>
          {/* Phase 07H (Figma 329:130): canonical MONOA app-icon geometry, not
              the simplified circle mark this screen used before. Fixed literal
              fills on purpose — a brand mark, not a themed surface. */}
          <div className={styles.brandRow}>
            <svg className={styles.mark} viewBox="0 0 100 100" aria-hidden="true">
              <rect width="100" height="100" rx="22.5" fill="#1b2130" />
              <rect x="8" y="8" width="84" height="84" rx="20" fill="#FF9B71" />
              <rect x="8" y="8" width="84" height="72" rx="20" fill="#99d9e0" />
              <rect x="20" y="20" width="60" height="58" rx="16" fill="#fdfbf7" />
              <rect x="32" y="38" width="10" height="22" rx="5" fill="#1b2130" />
              <rect x="58" y="38" width="10" height="22" rx="5" fill="#1b2130" />
              <path d="M 42 66 Q 50 72 58 66" stroke="#1b2130" strokeWidth="3.5" strokeLinecap="round" fill="none" />
            </svg>
            <span className={styles.wordmark}>MONOA</span>
          </div>
          <div>
            <h1 className={styles.heading}>Your day, in one place.</h1>
            <p className={styles.sub}>
              {isLogin ? "Sign in to continue." : "Create an account to get started."}
            </p>
          </div>
        </div>

        <form onSubmit={handleSubmit} className={styles.form}>
          {!isLogin && (
            <div className={styles.field}>
              <span className={styles.fieldIcon}>
                <User size={18} strokeWidth={2} aria-hidden="true" />
              </span>
              <input
                type="text"
                placeholder="Full name (optional)"
                value={name}
                onChange={(e) => setName(e.target.value)}
                autoComplete="name"
                className={styles.input}
              />
            </div>
          )}

          <div className={styles.field}>
            <span className={styles.fieldIcon}>
              <Mail size={18} strokeWidth={2} aria-hidden="true" />
            </span>
            <input
              type="email"
              placeholder="Email address"
              required
              value={email}
              onChange={(e) => setEmail(e.target.value)}
              autoComplete="email"
              className={styles.input}
            />
          </div>

          <div className={styles.field}>
            <span className={styles.fieldIcon}>
              <Lock size={18} strokeWidth={2} aria-hidden="true" />
            </span>
            <input
              type="password"
              placeholder={isLogin ? "Password" : "Password (at least 8 characters)"}
              required
              minLength={isLogin ? undefined : 8}
              value={password}
              onChange={(e) => setPassword(e.target.value)}
              autoComplete={isLogin ? "current-password" : "new-password"}
              className={styles.input}
            />
          </div>

          {errorMsg && (
            <p className={styles.error} role="alert">
              {errorMsg}
            </p>
          )}

          <button type="submit" disabled={loading} className={styles.submit}>
            {loading ? (
              <span className={styles.spinner} aria-hidden="true" />
            ) : (
              <>
                {isLogin ? "Sign In" : "Create Account"}
                <ArrowRight size={18} strokeWidth={2.25} aria-hidden="true" />
              </>
            )}
          </button>

          <button
            type="button"
            disabled={loading}
            onClick={() => {
              setIsLogin(!isLogin);
              setErrorMsg("");
            }}
            className={styles.secondary}
          >
            {isLogin ? "Create an account instead" : "Sign in instead"}
          </button>
        </form>
      </div>
    </div>
  );
}
