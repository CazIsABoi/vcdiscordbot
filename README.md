# VCDiscordBot

A "join-to-create" temporary voice channel bot, similar to VoiceMaster: members
join a designated **create-vc** channel and instantly get their own temporary
voice channel, which is deleted automatically once everyone leaves.

## Features

- Configurable join-to-create channel and category (per server)
- Automatic temp channel creation + cleanup when empty
- Owner controls via slash commands: lock, unlock, user limit, rename, kick,
  permit/reject specific members, transfer ownership, claim an abandoned channel
- SQLite persistence for server configuration and channel ownership

## Terms of Service / Privacy Policy

`docs/` contains a Terms of Service and Privacy Policy for the Bot, published
for free via GitHub Pages — no separate hosting needed. To turn it on:

1. On GitHub, go to the repo's **Settings** → **Pages**.
2. Under **Build and deployment**, set **Source** to **Deploy from a branch**.
3. Set **Branch** to this branch (or `main`, once merged) and the folder to **/docs**, then **Save**.
4. After a minute, the pages are live at:
   - `https://<your-github-username>.github.io/<repo-name>/terms.html`
   - `https://<your-github-username>.github.io/<repo-name>/privacy.html`

Paste those URLs into the Discord Developer Portal (**your app → General
Information → Terms of Service URL / Privacy Policy URL**). The pages'
contact links point at this repo's GitHub Issues — edit `docs/terms.html`
and `docs/privacy.html` if you'd rather list a different contact method.

## Commands

| Command | Who | Description |
|---|---|---|
| `/tempvc-setup init` | Admin (Manage Server) | Creates a category + join channel and enables the system |
| `/tempvc-setup use-existing` | Admin | Uses an existing voice channel as the join channel |
| `/tempvc-setup disable` | Admin | Disables the system for the server |
| `/lock` / `/unlock` | Channel owner | Locks/unlocks the channel to new joins |
| `/limit <n>` | Channel owner | Sets the user limit (0 = unlimited) |
| `/rename <name>` | Channel owner | Renames the channel |
| `/kick <member>` | Channel owner | Removes a member from the channel |
| `/permit <member>` / `/reject <member>` | Channel owner | Allows/blocks a specific member from joining a locked channel |
| `/transfer <member>` | Channel owner | Hands ownership to another member currently in the channel |
| `/claim` | Anyone | Claims ownership of a temp channel if the owner has left |

## Discord bot setup

1. Create an application at the [Discord Developer Portal](https://discord.com/developers/applications).
2. Under **Bot**, create a bot user, copy its token, and enable the
   **Server Members Intent** (used to resolve member display names).
3. Under **OAuth2 > URL Generator**, select the `bot` and `applications.commands`
   scopes, and grant these permissions: `Manage Channels`, `Move Members`,
   `View Channels`, `Connect`. Use the generated URL to invite the bot.

## Running locally

```bash
python3 -m venv .venv && source .venv/bin/activate
pip install -r requirements.txt
cp .env.example .env   # then fill in DISCORD_TOKEN
python -m bot.main
```

Set `DEV_GUILD_ID` in `.env` to your test server's ID while developing —
guild-scoped slash commands update instantly instead of taking up to an hour.

## Running with Docker

```bash
cp .env.example .env   # then fill in DISCORD_TOKEN
docker compose up --build -d
docker compose logs -f
```

The SQLite database is persisted to `./data` on the host via a bind mount.

## Hosting cheaply on Azure (no terminal required)

You don't need Docker, the Azure CLI, or a local terminal for this path —
GitHub builds the container image for you, and everything else is clicking
through the [Azure Portal](https://portal.azure.com) website.

### 1. Let GitHub build the image

This repo includes `.github/workflows/docker-publish.yml`, which automatically
builds the Docker image and publishes it to GitHub Container Registry (GHCR)
every time you push to `main` (you can also trigger it manually from the
**Actions** tab → *Build and publish Docker image* → *Run workflow*).

After it runs once (check the **Actions** tab for a green check), make the
published package public so Azure can pull it without credentials:
1. Go to your GitHub profile → **Packages** (or the repo's right sidebar → **Packages**).
2. Open the `vcdiscordbot` package → **Package settings** → **Change visibility** → **Public**.

Your image URL is `ghcr.io/<your-github-username>/<repo-name>:latest`, all
**lowercase** (Docker image names can't have capital letters, e.g.
`CazIsABoi` → `cazisaboi` — the workflow lowercases this for you automatically).

### 2. Deploy a Container Instance from the Portal

1. Sign into [portal.azure.com](https://portal.azure.com) with your school account.
2. Search for **Container Instances** → **Create**.
3. **Basics** tab:
   - Resource group: create new, e.g. `vcbot-rg`
   - Container name: `vcdiscordbot`
   - Region: pick one close to you
   - Image source: **Other registry**
   - Image: `ghcr.io/<your-github-username>/<repo-name>:latest` (lowercase)
   - Size: change to the smallest option, **1 vCPU / 1 GB** (or use "See all sizes" to go lower if offered) — this is what keeps the cost down
4. **Networking** tab: default settings are fine (public IP isn't needed, but leaving it doesn't cost extra).
5. **Advanced** tab → **Environment variables**: add one row:
   - Name: `DISCORD_TOKEN`, Value: your bot's token, and toggle it as a **Secure value**.
6. Click **Review + create**, then **Create**.

Azure will pull the image and start the bot. Within a minute or two it should
show as **Online** in your Discord server member list.

### 3. Check it's running / troubleshoot

In the Container Instance's page in the Portal:
- **Containers** → **Logs** tab shows the bot's console output (look for
  `Logged in as ...`).
- If it's crash-looping, check the logs for a Python traceback — the most
  common cause is a missing/incorrect `DISCORD_TOKEN`.

### Cost

At 1 vCPU / 1 GB run 24/7 this typically lands around **$30-35/month** on pay-as-you-go
pricing, or roughly half that at 0.5 vCPU / 0.5 GB if the Portal's size picker
lets you go that low — check the [Azure pricing calculator](https://azure.microsoft.com/pricing/calculator/)
for your region. Your school credit should comfortably cover this for a
semester; keep an eye on **Cost Management + Billing** in the Portal so you
don't run out unexpectedly.

Note that ACI's filesystem is ephemeral: if the container restarts, `tempvc.db`
(server config + channel ownership) resets and you'll need to re-run
`/tempvc-setup`. Fine for casual use; see "Notes on persistence" below if you
want it to survive restarts.

### If you're comfortable with a terminal later

`azure/deploy-aci.sh` and the Azure CLI (`az`) do the same thing as above from
the command line, and Azure has a browser-based terminal called **Cloud
Shell** (the `>_` icon in the Portal's top bar) if you ever want a "bash" that
doesn't require installing anything locally.

### Why not Azure Container Apps' scale-to-zero?

Container Apps' free consumption tier is attractive, but scale-to-zero doesn't
work for this bot: Discord bots hold a persistent gateway connection, so the
container must stay at `min-replicas: 1` at all times, which puts the cost in
the same ballpark as ACI without the scale-to-zero benefit.

## Notes on persistence

`guild_config` (which channel/category is configured) and `temp_channels`
(current temp channels and their owners) live in SQLite at `DATABASE_PATH`.
If the process restarts while temp channels exist, ownership tracking for
those channels is lost — they'll still get cleaned up once empty via the
`voice_states` intent, but owner commands won't recognize them as
temp channels until a fresh one is created. Mount a persistent volume (Azure
Files, or a VM's disk) in production if this matters to you.
