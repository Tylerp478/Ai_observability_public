# Deploying to AWS Lightsail

Target: one Lightsail instance running Postgres, the backend and the frontend
under Docker Compose, reachable from your phone and laptops over Tailscale and
from nowhere else.

**Why this shape.** `S3Storage` in `backend/src/obs_backend/storage.py` is still
a stub, so the trace store is Parquet + a WAL on a real disk — which rules out
anything with an ephemeral filesystem. The WAL compactor also runs as a
background thread (`main.py`, `_background_loop`), so the process has to stay
up rather than wake per request. That adds up to "one small always-on box with
a disk", which is what this is.

**Why Tailscale rather than a public hostname.** CLAUDE.md requires the app not
be open to the public internet. Tailscale gives you a real HTTPS certificate
and a stable hostname without opening a single port, so the login page is never
exposed to be brute-forced. Compose reinforces this: `web` binds `127.0.0.1`
only, so even a misconfigured firewall exposes nothing.

---

## 1. Create the instance

Lightsail console → Create instance:

- **Platform:** Linux/Unix → **Ubuntu 24.04 LTS**
- **Plan:** the **2 GB / 2 vCPU / 60 GB** bundle ($12/mo)
- Name it something like `obs`

Do **not** take the 512 MB or 1 GB bundle. `next build` will not complete in
512 MB, and 1 GB leaves no room for Postgres and DuckDB alongside it.

If you have never used Lightsail on this account, this bundle is covered by the
90-day free trial.

Then **Networking → IPv4 Firewall**: delete every rule except **SSH (TCP 22)**.
Nothing else should be reachable. (Once Tailscale SSH is working in step 4 you
can delete that rule too.)

## 2. Swap

A 2 GB box builds the Next.js image with very little headroom. Swap is what
stops the build being OOM-killed halfway through.

```bash
sudo fallocate -l 2G /swapfile && sudo chmod 600 /swapfile && sudo mkswap /swapfile && sudo swapon /swapfile && echo '/swapfile none swap sw 0 0' | sudo tee -a /etc/fstab
```

## 3. Docker

Wait for first boot to finish first. Ubuntu runs cloud-init and
`unattended-upgrades` for the first few minutes after a new instance starts,
and they hold the dpkg lock the whole time:

```bash
sudo cloud-init status --wait
```

Skipping this is the single most confusing failure in this runbook. The Docker
install script runs `apt-get -qq … >/dev/null`, so every apt message —
including "waiting for the lock" — is discarded, and a blocked install looks
exactly like a frozen terminal with no output at all. If you think it has hung,
check with `sudo fuser -v /var/lib/dpkg/lock-frontend` from a second session
rather than killing it; an interrupted dpkg needs `sudo dpkg --configure -a`
to recover.

Then, as separate commands so you can see which one stops:

```bash
curl -fsSL https://get.docker.com -o get-docker.sh && sudo sh get-docker.sh
```

```bash
sudo usermod -aG docker $USER
```

Now **log out and back in** — that is what picks up the new group. (Don't reach
for `newgrp docker`: it spawns a nested interactive shell, which looks like a
hang and only applies to that shell.)

Verify, without `sudo`:

```bash
docker run --rm hello-world
```

## 4. Tailscale

```bash
curl -fsSL https://tailscale.com/install.sh | sh && sudo tailscale up --ssh
```

Follow the printed URL to authenticate. `--ssh` lets you drop the public SSH
rule afterwards and reach the box over the tailnet instead.

Note the machine's tailnet name — `tailscale status` shows it, something like
`obs.tail1234.ts.net`.

## 5. Get the code onto the box

```bash
git clone https://github.com/<you>/AI_Observability_Project.git && cd AI_Observability_Project
```

If the repo is private, create a read-only **deploy key** in the instance
(`ssh-keygen -t ed25519`, then add the public half under the repo's Settings →
Deploy keys) and clone over SSH instead. Don't put a personal access token on
the box.

## 6. Configure

```bash
cp .env.example .env && nano .env
```

Set these — the rest of `.env.example` can keep its defaults:

| Variable | Value |
|---|---|
| `ANTHROPIC_API_KEY` | your key — the backend refuses to start without it |
| `ADMIN_EMAIL` | your login email |
| `ADMIN_PASSWORD` | a strong one from your password manager |
| `POSTGRES_PASSWORD` | `openssl rand -hex 32` — must be URL-safe, see below |

`POSTGRES_PASSWORD` is commented out in `.env.example` because it only applies
to the deployed stack — uncomment it and generate a value with:

```bash
openssl rand -hex 32
```

> **Use hex, not base64.** Compose interpolates this password into
> `OBS_DATABASE_URL` (`postgresql://obs:PASSWORD@db:5432/obs`), and base64
> output contains `/` and `+`, which truncate the URL's authority section. The
> backend then fails with `failed to resolve host 'obs'` — an error that points
> nowhere near the actual cause. Hex is always URL-safe.

Everything else (`OBS_HOST`, `OBS_DATABASE_URL`, `OBS_DATA_DIR`,
`OBS_COOKIE_SECURE`) is set by `compose.yaml` and will override whatever `.env`
says, so leave those alone.

> **`ADMIN_PASSWORD` is enforced, not advisory.** Compose sets
> `OBS_HOST=0.0.0.0`, and `config.py::check_password_strength` refuses to boot
> on a non-loopback bind with a password under 8 characters or one on the
> common-wordlist. If the backend exits at startup, read its logs — this is
> usually why.

## 7. Bring it up

```bash
docker compose up -d --build
```

First build takes several minutes. Then check all three are healthy:

```bash
docker compose ps && docker compose logs backend --tail=30
```

You want `db` and `backend` showing `healthy`, and the backend log ending in
uvicorn's startup line rather than a traceback.

## 8. Publish it to your tailnet

Serve is **disabled by default on a new tailnet**, so this is a two-part step.

```bash
sudo tailscale serve --bg 3000
```

The first run will likely fail with "Serve is not enabled on your tailnet" and
print a `https://login.tailscale.com/f/serve?node=…` link. Open that in a
browser and enable it — that also switches on MagicDNS and HTTPS certificates
if they aren't already, which is what issues the certificate. Then run the
command again.

That puts the frontend on `https://<machine>.<tailnet>.ts.net` with a real
certificate. `tailscale serve status` confirms it.

Open that URL on your phone with Tailscale connected and sign in.

> Use the **HTTPS tailnet hostname**, not `http://<tailscale-ip>:3000`.
> `OBS_COOKIE_SECURE=true` means the session cookie is only sent over HTTPS —
> over plain HTTP the login will appear to succeed and then bounce you
> straight back to the login page.

## 9. Ingest key for the SDK

Only needed if you want to send traces from outside the box:

```bash
docker compose exec backend uv run scripts/bootstrap.py
```

It prints the plaintext key exactly once — it's stored as a SHA-256 hash and
cannot be recovered. Put it in the *client's* `.env` as `OBS_API_KEY`, and
point `OBS_OTLP_ENDPOINT` at `https://<machine>.<tailnet>.ts.net/v1/traces`.

---

## Backups

Two things matter, and one of them is not a database:

```bash
# Postgres: prompts, datasets, scorers, runs, users, keys
docker compose exec -T db pg_dump -U obs obs | gzip > obs-$(date +%F).sql.gz

# The trace store: Parquet + WAL. This is not in Postgres.
# Confirm the volume name first — compose derives it from the directory name:
#   docker volume ls | grep obsdata
docker run --rm -v ai_observability_project_obsdata:/data -v $PWD:/backup alpine \
  tar czf /backup/obsdata-$(date +%F).tar.gz -C /data .
```

Copy both off the box. A rebuilt instance without `obsdata` comes up with a
working app and zero traces.

## Updating

```bash
git pull && docker compose up -d --build
```

## If something breaks

| Symptom | Look at |
|---|---|
| Backend restarts in a loop | `docker compose logs backend` — usually a weak `ADMIN_PASSWORD` or a missing `ANTHROPIC_API_KEY` |
| `failed to resolve host 'obs'` | `POSTGRES_PASSWORD` isn't URL-safe. Regenerate with `openssl rand -hex 32`, then `docker compose down -v` — the old password is baked into the existing `pgdata` volume |
| Password change didn't take | Postgres only reads `POSTGRES_PASSWORD` when it *initialises* the volume. Changing it later leaves the database on the old one and the backend can't authenticate; `docker compose down -v` starts clean (and destroys the data) |
| Every `/api` call 500s, `ECONNREFUSED 127.0.0.1:8000` | The image was built without the `BACKEND_ORIGIN` build arg. `docker compose build --no-cache web` |
| Login bounces back to the login page | You're on `http://`, not the HTTPS tailnet hostname |
| Build killed partway through | Swap missing — redo step 2 |
| Docker install sits there with no output | apt is blocked on the dpkg lock and the script discards its output. `sudo fuser -v /var/lib/dpkg/lock-frontend` from a second session; wait it out, don't kill it |
| `permission denied` on the docker socket | The group change needs a fresh login — log out and back in |
| `Serve is not enabled on your tailnet` | Expected on a new tailnet. Open the `login.tailscale.com/f/serve?node=…` link the CLI prints, enable it, re-run |
| Site unreachable | `tailscale status` on both the box and the phone; `tailscale serve status` on the box |
| Tempted to use `http://<tailscale-ip>:3000` | It won't work — compose binds the frontend to 127.0.0.1, and `OBS_COOKIE_SECURE=true` blocks the session cookie over plain HTTP. Use the HTTPS tailnet hostname |
| "Can't reach the backend" in the UI | `docker compose ps` — the `backend` container is down or unhealthy |
