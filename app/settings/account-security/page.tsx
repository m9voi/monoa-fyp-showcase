"use client";

// Account & Security — Change Password, Change Email, Delete Account.
// Referenced (but explicitly NOT built) by app/settings/page.tsx's own
// top-of-file comment; this is that real destination.
//
// All three actions re-send the current password even though the page is
// already behind AuthGuard/the session cookie — the backend re-verifies it
// server-side before taking effect (see main.py's _verify_current_password).
// Change Email has no second-channel (email) verification: this project has
// no email-sending provider wired in, so the change takes effect immediately
// on a correct password. That's a real, disclosed limitation, not something
// this page should imply is a verified-email flow.

import React, { useState } from "react";
import Link from "next/link";
import { ChevronLeft, Check, AlertTriangle } from "lucide-react";
import { useAuth } from "../../lib/auth";
import { apiFetch, ApiError, describeFailure } from "../../lib/api";
import type { AuthUser } from "../../lib/types";

const DISPLAY_FONT = 'var(--font-bricolage), "Bricolage Grotesque", sans-serif';
const BODY_FONT = 'var(--font-hanken), "Hanken Grotesk", sans-serif';

const inputClass =
  "mt-1 w-full bg-field border border-field-border rounded-[18px] px-4 h-[52px] text-[15px] text-primary outline-none focus:border-ring";

function Field({
  label,
  ...props
}: { label: string } & React.InputHTMLAttributes<HTMLInputElement>) {
  return (
    <label className="block">
      <span className="text-xs text-muted" style={{ fontFamily: BODY_FONT }}>{label}</span>
      <input className={inputClass} style={{ fontFamily: BODY_FONT }} {...props} />
    </label>
  );
}

function Banner({ tone, children }: { tone: "ok" | "err"; children: React.ReactNode }) {
  return (
    <p
      role={tone === "err" ? "alert" : "status"}
      className={`flex items-center gap-1.5 text-sm rounded-[16px] px-3 py-2 border ${
        tone === "ok"
          ? "text-success bg-success/10 border-success/20"
          : "text-danger bg-danger/10 border-danger/20"
      }`}
      style={{ fontFamily: BODY_FONT }}
    >
      {tone === "ok" && <Check size={14} />} {children}
    </p>
  );
}

function Card({ title, subtitle, children }: { title: string; subtitle: string; children: React.ReactNode }) {
  return (
    <section className="bg-surface border border-line rounded-[20px] p-4 space-y-3.5">
      <div>
        <h2 className="text-[15px] font-semibold text-primary" style={{ fontFamily: BODY_FONT }}>{title}</h2>
        <p className="text-[11px] text-muted mt-0.5" style={{ fontFamily: BODY_FONT }}>{subtitle}</p>
      </div>
      {children}
    </section>
  );
}

function PasswordSection() {
  const [currentPassword, setCurrentPassword] = useState("");
  const [newPassword, setNewPassword] = useState("");
  const [confirmPassword, setConfirmPassword] = useState("");
  const [busy, setBusy] = useState(false);
  const [error, setError] = useState("");
  const [saved, setSaved] = useState(false);

  const submit = async (e: React.FormEvent) => {
    e.preventDefault();
    if (busy) return;
    setError("");
    setSaved(false);
    if (newPassword.length < 8) {
      setError("New password must be at least 8 characters.");
      return;
    }
    if (newPassword !== confirmPassword) {
      setError("New password and confirmation don't match.");
      return;
    }
    setBusy(true);
    try {
      await apiFetch("/api/auth/change-password", {
        method: "POST",
        kind: "auth",
        body: JSON.stringify({ current_password: currentPassword, new_password: newPassword }),
      });
      setCurrentPassword("");
      setNewPassword("");
      setConfirmPassword("");
      setSaved(true);
      setTimeout(() => setSaved(false), 3000);
    } catch (err) {
      setError(err instanceof ApiError ? describeFailure(err, { verb: "change your password" }) : "Couldn't change your password. Please try again.");
    } finally {
      setBusy(false);
    }
  };

  return (
    <Card title="Change password" subtitle="You'll stay signed in here; every other device is signed out.">
      <form onSubmit={submit} className="space-y-3">
        <Field
          label="Current password"
          type="password"
          autoComplete="current-password"
          value={currentPassword}
          onChange={(e) => { setCurrentPassword(e.target.value); setError(""); }}
          required
        />
        <Field
          label="New password (at least 8 characters)"
          type="password"
          autoComplete="new-password"
          minLength={8}
          value={newPassword}
          onChange={(e) => { setNewPassword(e.target.value); setError(""); }}
          required
        />
        <Field
          label="Confirm new password"
          type="password"
          autoComplete="new-password"
          value={confirmPassword}
          onChange={(e) => { setConfirmPassword(e.target.value); setError(""); }}
          required
        />
        {error && <Banner tone="err">{error}</Banner>}
        {saved && !error && <Banner tone="ok">Password changed.</Banner>}
        <button
          type="submit"
          disabled={busy || !currentPassword || !newPassword || !confirmPassword}
          className="w-full bg-[#1b2130] text-[#fafaf5] font-semibold py-3 rounded-[16px] disabled:opacity-50"
          style={{ fontFamily: BODY_FONT }}
        >
          {busy ? "Changing…" : "Change password"}
        </button>
      </form>
    </Card>
  );
}

function EmailSection({ currentEmail, onChanged }: { currentEmail: string; onChanged: (email: string) => void }) {
  const [newEmail, setNewEmail] = useState("");
  const [currentPassword, setCurrentPassword] = useState("");
  const [busy, setBusy] = useState(false);
  const [error, setError] = useState("");
  const [saved, setSaved] = useState(false);

  const submit = async (e: React.FormEvent) => {
    e.preventDefault();
    if (busy) return;
    setError("");
    setSaved(false);
    setBusy(true);
    try {
      const res = await apiFetch<{ user: AuthUser }>("/api/auth/change-email", {
        method: "POST",
        kind: "auth",
        body: JSON.stringify({ new_email: newEmail.trim(), current_password: currentPassword }),
      });
      onChanged(res.user.email);
      setNewEmail("");
      setCurrentPassword("");
      setSaved(true);
      setTimeout(() => setSaved(false), 3000);
    } catch (err) {
      setError(err instanceof ApiError ? describeFailure(err, { verb: "change your email" }) : "Couldn't change your email. Please try again.");
    } finally {
      setBusy(false);
    }
  };

  return (
    <Card title="Change email" subtitle="Takes effect immediately — MONOA doesn't send a verification email to confirm the new address.">
      <form onSubmit={submit} className="space-y-3">
        <label className="block">
          <span className="text-xs text-muted" style={{ fontFamily: BODY_FONT }}>Current email</span>
          <input
            type="email"
            value={currentEmail}
            readOnly
            aria-readonly="true"
            className="mt-1 w-full bg-inset border border-field-border rounded-[18px] px-4 h-[52px] text-[15px] text-secondary outline-none cursor-default"
            style={{ fontFamily: BODY_FONT }}
          />
        </label>
        <Field
          label="New email"
          type="email"
          autoComplete="email"
          value={newEmail}
          onChange={(e) => { setNewEmail(e.target.value); setError(""); }}
          required
        />
        <Field
          label="Current password"
          type="password"
          autoComplete="current-password"
          value={currentPassword}
          onChange={(e) => { setCurrentPassword(e.target.value); setError(""); }}
          required
        />
        {error && <Banner tone="err">{error}</Banner>}
        {saved && !error && <Banner tone="ok">Email changed.</Banner>}
        <button
          type="submit"
          disabled={busy || !newEmail || !currentPassword}
          className="w-full bg-[#1b2130] text-[#fafaf5] font-semibold py-3 rounded-[16px] disabled:opacity-50"
          style={{ fontFamily: BODY_FONT }}
        >
          {busy ? "Changing…" : "Change email"}
        </button>
      </form>
    </Card>
  );
}

function DeleteAccountSection() {
  const { logout } = useAuth();
  const [confirming, setConfirming] = useState(false);
  const [currentPassword, setCurrentPassword] = useState("");
  const [busy, setBusy] = useState(false);
  const [error, setError] = useState("");

  const submit = async (e: React.FormEvent) => {
    e.preventDefault();
    if (busy) return;
    setError("");
    setBusy(true);
    try {
      await apiFetch("/api/auth/delete-account", {
        method: "POST",
        kind: "auth",
        body: JSON.stringify({ current_password: currentPassword }),
      });
      // The account and every row it owns are gone server-side — clear local
      // auth state the same way an explicit sign-out does; AuthGuard then
      // redirects to /login on its own.
      await logout();
    } catch (err) {
      setError(err instanceof ApiError ? describeFailure(err, { verb: "delete your account" }) : "Couldn't delete your account. Please try again.");
      setBusy(false);
    }
  };

  return (
    <Card title="Delete account" subtitle="Permanently deletes your account and all of your data. This cannot be undone.">
      {!confirming ? (
        <button
          type="button"
          onClick={() => setConfirming(true)}
          className="w-full border border-danger/30 text-danger font-semibold py-3 rounded-[16px] hover:bg-danger/5"
          style={{ fontFamily: BODY_FONT }}
        >
          Delete my account
        </button>
      ) : (
        <form onSubmit={submit} className="space-y-3">
          <p className="flex items-start gap-2 text-[12px] text-danger bg-danger/10 border border-danger/20 rounded-[16px] px-3 py-2" style={{ fontFamily: BODY_FONT }}>
            <AlertTriangle size={14} className="mt-0.5 shrink-0" />
            This permanently deletes your events, tasks, wallet history and every other record tied to this account. Enter your password to confirm.
          </p>
          <Field
            label="Current password"
            type="password"
            autoComplete="current-password"
            value={currentPassword}
            onChange={(e) => { setCurrentPassword(e.target.value); setError(""); }}
            autoFocus
            required
          />
          {error && <Banner tone="err">{error}</Banner>}
          <div className="flex gap-2">
            <button
              type="button"
              onClick={() => { setConfirming(false); setCurrentPassword(""); setError(""); }}
              disabled={busy}
              className="flex-1 border border-line text-secondary font-semibold py-3 rounded-[16px] disabled:opacity-50"
              style={{ fontFamily: BODY_FONT }}
            >
              Cancel
            </button>
            <button
              type="submit"
              disabled={busy || !currentPassword}
              className="flex-1 bg-danger-solid text-[#fafaf5] font-semibold py-3 rounded-[16px] disabled:opacity-50"
              style={{ fontFamily: BODY_FONT }}
            >
              {busy ? "Deleting…" : "Yes, delete it"}
            </button>
          </div>
        </form>
      )}
    </Card>
  );
}

export default function AccountSecurityPage() {
  const { user } = useAuth();
  const [email, setEmail] = useState(user?.email ?? "");

  return (
    <div className="min-h-screen bg-app flex justify-center">
      <div className="w-full max-w-md min-h-screen relative flex flex-col">
        <div
          className="px-6 pb-32 space-y-5"
          style={{ paddingTop: "max(1.5rem, calc(env(safe-area-inset-top) + 0.5rem))" }}
        >
          <header className="flex items-center gap-4">
            <Link
              href="/settings"
              aria-label="Back to Settings"
              className="w-10 h-10 bg-surface border border-line rounded-full flex items-center justify-center text-secondary hover:text-primary transition-colors shrink-0"
            >
              <ChevronLeft size={20} />
            </Link>
            <h1
              className="text-[22px] leading-[1.15] font-extrabold text-primary"
              style={{ fontFamily: DISPLAY_FONT, fontVariationSettings: '"opsz" 14, "wdth" 100' }}
            >
              Account &amp; Security
            </h1>
          </header>

          <PasswordSection />
          <EmailSection currentEmail={email || user?.email || ""} onChanged={setEmail} />
          <DeleteAccountSection />
        </div>
      </div>
    </div>
  );
}
