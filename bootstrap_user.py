"""Set a password (and, if needed, a real login email) for an existing account.

Why this exists
---------------
Accounts that predate Phase 2 auth were migrated by 0006 with
`password_hash = NULL`, so they cannot log in. In particular the legacy local
account (id `mussab_admin_1`) has the email `admin@coreos.local`, and `.local`
is a reserved/special-use domain that is not a valid login email. This script
lets the account owner:

  * find that account by its current email (lenient lookup — no validation),
  * choose a NEW real login email (strict RFC validation), and
  * choose their own password (Argon2id-hashed; never invented / committed).

The account's `id` never changes, so every FK-linked row (transactions, tasks,
schedule events, recommendations, preferences, budgets, financial settings,
map records) stays attached to the same owner. Existing sessions are revoked.

Usage
-----
  # legacy account: must supply a real new email because admin@coreos.local
  # is not a usable login address
  python bootstrap_user.py --email admin@coreos.local --new-email you@yourdomain.com

  # an account that already has a valid email: password only
  python bootstrap_user.py --email you@yourdomain.com

  # non-interactive (CI): password from env, never argv / history
  COREOS_BOOTSTRAP_EMAIL=you@yourdomain.com \
  COREOS_BOOTSTRAP_PASSWORD='choose-a-strong-one' \
  python bootstrap_user.py
"""
import argparse
import getpass
import os
import sys

sys.path.insert(0, os.path.dirname(os.path.abspath(__file__)))

import db as db_layer          # noqa: E402
import auth as auth_core       # noqa: E402


def _find_account(conn, raw_email: str):
    key = raw_email.strip().lower()
    return conn.execute(
        "SELECT id, email, email_normalized, password_hash FROM users "
        "WHERE lower(trim(email)) = ? OR email_normalized = ?",
        (key, key),
    ).fetchone()


def main() -> int:
    parser = argparse.ArgumentParser(description="Set/reset a CoreOS account password.")
    parser.add_argument("--email", help="current account email (or COREOS_BOOTSTRAP_EMAIL)")
    parser.add_argument("--new-email", help="new real login email (required if the current one is reserved)")
    args = parser.parse_args()

    current_email = args.email or os.getenv("COREOS_BOOTSTRAP_EMAIL") or ""
    if not current_email.strip():
        print("ERROR: provide --email or COREOS_BOOTSTRAP_EMAIL.", file=sys.stderr)
        return 2

    new_email_norm = None
    if args.new_email:
        try:
            new_email_norm = auth_core.normalize_and_validate_email(args.new_email)
        except ValueError as e:
            print(f"ERROR: --new-email {e}", file=sys.stderr)
            return 2

    password = os.getenv("COREOS_BOOTSTRAP_PASSWORD")
    if not password:
        password = getpass.getpass("New password: ")
        if password != getpass.getpass("Confirm password: "):
            print("ERROR: passwords do not match.", file=sys.stderr)
            return 2
    try:
        auth_core.validate_password(password)
    except ValueError as e:
        print(f"ERROR: {e}", file=sys.stderr)
        return 2

    pw_hash = auth_core.hash_password(password)
    now = db_layer.iso_now()

    with db_layer.transaction() as conn:
        row = _find_account(conn, current_email)
        if row is None:
            print(f"ERROR: no account found for {current_email!r}.", file=sys.stderr)
            return 1

        # If the account's current email is not a valid login email, a new one
        # is mandatory.
        current_ok = True
        try:
            auth_core.normalize_and_validate_email(row["email"] or "")
        except ValueError:
            current_ok = False
        if not current_ok and not new_email_norm:
            print(f"ERROR: {row['email']!r} is not a usable login email. "
                  f"Re-run with --new-email <a real address>.", file=sys.stderr)
            return 2

        if new_email_norm:
            clash = conn.execute(
                "SELECT 1 FROM users WHERE email_normalized = ? AND id <> ?",
                (new_email_norm, row["id"]),
            ).fetchone()
            if clash:
                print(f"ERROR: {new_email_norm} is already used by another account.", file=sys.stderr)
                return 1
            conn.execute(
                "UPDATE users SET email = ?, email_normalized = ?, password_hash = ?, "
                "is_active = 1, updated_at = ? WHERE id = ?",
                (new_email_norm, new_email_norm, pw_hash, now, row["id"]),
            )
            final_email = new_email_norm
        else:
            conn.execute(
                "UPDATE users SET password_hash = ?, is_active = 1, updated_at = ? WHERE id = ?",
                (pw_hash, now, row["id"]),
            )
            final_email = row["email"]

        conn.execute("DELETE FROM sessions WHERE user_id = ?", (row["id"],))

    print(f"OK: account {row['id']} can now log in as {final_email!r}. "
          f"Existing sessions were revoked.")
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
