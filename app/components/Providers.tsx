"use client";

import React from "react";
import { AuthProvider } from "../lib/auth";
import ThemeProvider from "./ThemeProvider";
import PendingRecovery from "./PendingRecovery";

export default function Providers({ children }: { children: React.ReactNode }) {
  return (
    <AuthProvider>
      <ThemeProvider>
        {children}
        <PendingRecovery />
      </ThemeProvider>
    </AuthProvider>
  );
}
