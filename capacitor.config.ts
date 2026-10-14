import type { CapacitorConfig } from '@capacitor/cli';

const config: CapacitorConfig = {
  appId: 'com.m9voi.monoa',
  appName: 'MONOA',
  webDir: 'public',
  // EXPERIMENTAL (Checkpoint 4): this iOS shell is an early native-packaging
  // proof, not a shipped/installable app. It previously pointed `server.url`
  // at an ephemeral Cloudflare Quick Tunnel for a one-off physical-device
  // test; that hostname expires the moment the tunnel process restarts, so it
  // is never meaningful to commit. To reproduce that test, start your own
  // tunnel (or local HTTPS dev server) and set `server.url` to it locally —
  // do not commit a real hostname here. HTTPS only, no cleartext traffic.
};

export default config;
