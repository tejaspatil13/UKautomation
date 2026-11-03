# Licensing: set-up and administration

Copilot's trial and activation are controlled by a small licence server that you own. It runs on
**Cloudflare Workers with a D1 database**, on the free plan: about 48 checks per client per day,
against a free allowance of 100,000 requests per day. You control everything from your Mac with
`license-server/admin.py`.

## How it works

- On first start, the app sends the server an anonymous id for the computer: a SHA-256 hash of the
  Windows MachineGuid. The server records **that moment, on its own clock**, as the start of the
  trial.
- The server answers with a token signed with its **private** Ed25519 key, which never leaves
  Cloudflare. The app contains only the **public** key, so it can check tokens but can't make them.
- A token is valid only for that computer and that server environment (test or production).
- Clients check at start-up, every 5 minutes, and whenever the user chooses **Check again**. Test
  builds check every minute.
- Offline, a trial keeps working for up to 24 hours since its last check, and never past its end.
  A paid licence works offline for up to 14 days.
- Turning the PC's clock back doesn't extend access. Online, only the server's clock counts.
  Offline, the latest time Copilot has ever seen is remembered.
- Reinstalling Copilot or deleting its files doesn't restart the trial: the server remembers the
  computer. Only reinstalling Windows changes the id.
- **Test and production are completely separate**, each with its own database, keys and admin
  token. A test build refuses to run against the production server, and the reverse; the build
  checks this. In production the trial is always exactly 48 hours, fixed in the server code.

## One-time set-up (about 20 minutes)

You need a free Cloudflare account. Run everything from the `license-server` folder:

```bash
cd license-server
npm install
npx wrangler login                                   # opens the browser once

# 1. Two databases. Paste each database_id it prints into wrangler.toml (test, then production).
npx wrangler d1 create copilot-license-test
npx wrangler d1 create copilot-license
npm run db:init:test
npm run db:init:production

# 2. Signing keys: one pair per environment. Run twice, keeping the output of each.
../.venv/bin/python admin.py keygen

# 3. Secrets (each command asks you to paste the value)
npx wrangler secret put SIGNING_KEY --env test        # the test SIGNING_KEY from keygen
npx wrangler secret put ADMIN_TOKEN --env test        # a long random password, see below
npx wrangler secret put SIGNING_KEY --env production  # the production SIGNING_KEY (the other keygen run)
npx wrangler secret put ADMIN_TOKEN --env production  # a different long random password

# 4. Go live
npm run deploy:test          # prints https://copilot-license-test.<your-subdomain>.workers.dev
npm run deploy:production    # prints https://copilot-license.<your-subdomain>.workers.dev
```

To make an admin token: `python3 -c "import secrets; print(secrets.token_urlsafe(32))"`.
Keep both admin tokens and both SIGNING_KEYs in a password manager, and nowhere else. If you lose
a SIGNING_KEY, make a new pair and release a new build with the new public key.

**5. Tell the Windows build where the servers are.** In GitHub, open the repository's
**Settings → Secrets and variables → Actions → Variables** and add these. They are not secret:

| Variable | Value |
|---|---|
| `LICENSE_URL_TEST` | the test URL from step 4 |
| `LICENSE_PUBLIC_KEY_TEST` | the test PUBLIC KEY from keygen |
| `LICENSE_URL_PRODUCTION` | the production URL from step 4 |
| `LICENSE_PUBLIC_KEY_PRODUCTION` | the production PUBLIC KEY from keygen |

**6. Recommended:** in **Settings → Environments**, create **production** and add yourself as a
required reviewer. Every client release then waits for your approval.

## Your servers (set up 2 Nov 2026)

| | Address |
|---|---|
| Test | https://copilot-license-test.copilot-license-server.workers.dev |
| Production | https://copilot-license.copilot-license-server.workers.dev |

The signing keys and admin tokens are in `~/.copilot-license/secrets.env` on the Mac (readable
only by you, outside the repository). Copy them into a password manager. To load them for
`admin.py` in a terminal:

```bash
set -a; . ~/.copilot-license/secrets.env; set +a
```

## Daily administration

Or set these yourself once per terminal session (or in your shell profile):

```bash
export COPILOT_LICENSE_URL_TEST=https://copilot-license-test.<your-subdomain>.workers.dev
export COPILOT_ADMIN_TOKEN_TEST=<test admin token>
export COPILOT_LICENSE_URL_PRODUCTION=https://copilot-license.<your-subdomain>.workers.dev
export COPILOT_ADMIN_TOKEN_PRODUCTION=<production admin token>
```

Name a computer by the first few characters of its id, as shown by `list`:

| To… | Run |
|---|---|
| See every computer, its status and time left | `python license-server/admin.py --env production list` |
| Convert to paid, permanent | `… --env production paid 3f2a` |
| Paid until a date | `… --env production paid 3f2a 2027-11-30T23:59:00Z` |
| Give more trial time | `… --env production extend 3f2a 24` (hours) |
| Set an exact end | `… --env production set-expiry 3f2a 2026-11-20T18:00:00Z` |
| End access now | `… --env production expire 3f2a` |
| Turn access off / back on | `… --env production revoke 3f2a` / `unrevoke 3f2a` |
| Add a note | `… --env production note 3f2a "ACME Ltd, invoice 1042"` |
| Forget a computer (next start = new trial) | `… --env test reset 3f2a` |

Production changes ask you to type `yes` first. A client picks up a change within 5 minutes, at
its next start, or immediately when they choose **Check again**. **The client never needs to
reinstall:** paying simply means you run `paid`.

## Testing the trial on your Windows test PC

Install the **test** build (the latest test pre-release, see the developer guide). Its first start
creates a 48-hour trial on the **test** server, like production, so testers can use it normally.
Then, to try each case on one PC:

| Scenario | Command | What you should see within about a minute |
|---|---|---|
| Trial ending in a few minutes | `--env test trial <id> 5` | 5 minutes later: "Your free trial has ended" |
| Trial of one hour | `--env test trial <id> 60` | works for an hour, then the expired screen |
| Real 48-hour trial | `--env test trial <id> 2880` | works for 2 days |
| Expired trial | `--env test expire <id>` | expired screen; new searches refused |
| Revoked licence | `--env test revoke <id>` | "Access has been turned off" |
| Paid / permanent | `--env test paid <id>` | unlocks by itself, with no reinstall |

Repeat any scenario with `reset`. Nothing here can affect the client, because the client's app
only trusts the production server.

## What this protects against, and what it doesn't

It stops the usual tricks: changing the clock, reinstalling, deleting files, copying the program to
another PC, or editing the licence file (the signature check fails).

No licence scheme can stop someone who is determined to modify the program itself. Copilot is a
Python program, and **this repository is public**, so its code can be read. If that matters, make the
repository private. Updates would then need another download location, such as Cloudflare R2;
ask before changing this. In return, the scheme keeps no secrets in the app or the repository:
the signing key and admin tokens exist only in Cloudflare and your password manager.
