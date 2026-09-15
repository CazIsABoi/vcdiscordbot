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

## Hosting cheaply on Azure

The bot only needs to hold one persistent WebSocket connection to Discord and
handle occasional voice-state events, so it runs comfortably on the smallest
compute Azure offers. Two options, cheapest first:

### Option A: Azure Container Instances (ACI)

Simplest option, billed per second, no VM to patch. Push the image to a
registry (Azure Container Registry's free/Basic tier works), then deploy:

```bash
az login
az acr create --resource-group vcbot-rg --name <yourregistry> --sku Basic
az acr login --name <yourregistry>
docker build -t <yourregistry>.azurecr.io/vcdiscordbot:latest .
docker push <yourregistry>.azurecr.io/vcdiscordbot:latest

DISCORD_TOKEN=your-token ./azure/deploy-aci.sh vcbot-rg <yourregistry>.azurecr.io/vcdiscordbot:latest
```

At the smallest size (0.5 vCPU / 0.5 GB) run 24/7, this typically lands in the
**$10-20/month** range — check the [Azure pricing calculator](https://azure.microsoft.com/pricing/calculator/)
for current rates in your region. Note that ACI's filesystem is ephemeral: if
the container restarts, `data/tempvc.db` (server config + channel ownership)
resets and you'll need to re-run `/tempvc-setup`. For a bot on one or two
servers this is a minor inconvenience; if you want it to survive restarts,
mount an Azure Files share as the `/app/data` volume (`az container create
--azure-file-volume-*` flags).

### Option B: A small burstable VM (cheapest for always-on + persistence)

A `Standard_B1s` VM (1 vCPU, 1 GB RAM) is usually the cheapest way to get a
real, persistent disk alongside 24/7 uptime — typically **$7-10/month** pay-as-you-go,
less with a 1-year reserved instance or your Azure free credit.

```bash
az vm create \
  --resource-group vcbot-rg \
  --name vcbot-vm \
  --image Ubuntu2404 \
  --size Standard_B1s \
  --admin-username azureuser \
  --generate-ssh-keys

# SSH in, install Docker, then:
git clone <your fork of this repo>
cd VCDiscordBot
cp .env.example .env   # fill in DISCORD_TOKEN
docker compose up --build -d
```

`docker-compose.yml` sets `restart: unless-stopped`, so the bot comes back up
automatically after a VM reboot.

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
