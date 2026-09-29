from __future__ import annotations

import asyncio
import logging
import os
import re
import time

import aiohttp
import discord
from discord import app_commands
from discord.ext import commands, tasks

from ..db import Database, GitHubLink

log = logging.getLogger("tempvc.github")

GITHUB_API = "https://api.github.com"
GITHUB_HEADERS = {"Accept": "application/vnd.github+json", "X-GitHub-Api-Version": "2022-11-28"}

# Each link holds one of the parent channel's 15 webhook slots, so links that
# go quiet get a warning and are then removed to free the slot.
INACTIVE_AFTER = 30 * 86400
GRACE_PERIOD = 7 * 86400
MAX_LINKS_PER_THREAD = 5
MAX_WEBHOOKS_REACHED = 30007  # Discord error code

# Interaction tokens die after 15 minutes; stop polling a little before that
# so the final followup can still be delivered.
MAX_SIGN_IN_WAIT = 14 * 60

# aiohttp signals timeouts with asyncio.TimeoutError, which isn't a ClientError.
NETWORK_ERRORS = (aiohttp.ClientError, asyncio.TimeoutError)

REPO_RE = re.compile(
    r"^(?:https?://github\.com/)?([A-Za-z0-9](?:[A-Za-z0-9-]*[A-Za-z0-9])?)/([A-Za-z0-9._-]+?)(?:\.git)?/?$"
)


def parse_repo(value: str) -> str | None:
    match = REPO_RE.match(value.strip())
    return f"{match[1]}/{match[2]}" if match else None


class GitHubAuthError(Exception):
    pass


class GitHubLinks(commands.Cog):
    """Link a GitHub repo to a thread so its pushes are posted there."""

    github_group = app_commands.Group(
        name="github", description="Post a GitHub repo's commits in a thread.", guild_only=True
    )

    def __init__(self, bot: commands.Bot, db: Database):
        self.bot = bot
        self.db = db
        self.client_id = os.getenv("GITHUB_CLIENT_ID") or None
        self.client_secret = os.getenv("GITHUB_CLIENT_SECRET") or None
        self.http: aiohttp.ClientSession | None = None
        self._signing_in: set[int] = set()

    async def cog_load(self) -> None:
        self.http = aiohttp.ClientSession(timeout=aiohttp.ClientTimeout(total=30))
        self.cleanup.start()

    async def cog_unload(self) -> None:
        self.cleanup.cancel()
        if self.http is not None:
            await self.http.close()

    # ---------- GitHub sign-in (device flow) and API ----------

    async def _start_device_flow(self) -> dict:
        async with self.http.post(
            "https://github.com/login/device/code",
            data={"client_id": self.client_id, "scope": "admin:repo_hook"},
            headers={"Accept": "application/json"},
        ) as resp:
            data = await resp.json(content_type=None)
        if "device_code" not in data:
            raise GitHubAuthError(data.get("error_description") or "GitHub didn't return a sign-in code.")
        return data

    async def _wait_for_token(self, flow: dict) -> str:
        interval = flow.get("interval", 5)
        deadline = time.monotonic() + min(flow.get("expires_in", 900), MAX_SIGN_IN_WAIT)
        while time.monotonic() < deadline:
            await asyncio.sleep(interval)
            async with self.http.post(
                "https://github.com/login/oauth/access_token",
                data={
                    "client_id": self.client_id,
                    "device_code": flow["device_code"],
                    "grant_type": "urn:ietf:params:oauth:grant-type:device_code",
                },
                headers={"Accept": "application/json"},
            ) as resp:
                data = await resp.json(content_type=None)

            if "access_token" in data:
                return data["access_token"]
            error = data.get("error")
            if error == "authorization_pending":
                continue
            if error == "slow_down":
                interval = data.get("interval", interval + 5)
                continue
            if error == "access_denied":
                raise GitHubAuthError("The GitHub sign-in was cancelled.")
            if error == "expired_token":
                break
            raise GitHubAuthError(data.get("error_description") or f"GitHub sign-in failed ({error}).")
        raise GitHubAuthError("The sign-in code expired. Run the command again to get a new one.")

    async def _revoke_token(self, token: str) -> None:
        # Without the client secret the token can't be revoked, only forgotten.
        if not self.client_secret:
            return
        try:
            async with self.http.delete(
                f"{GITHUB_API}/applications/{self.client_id}/token",
                json={"access_token": token},
                headers=GITHUB_HEADERS,
                auth=aiohttp.BasicAuth(self.client_id, self.client_secret),
            ) as resp:
                if resp.status != 204:
                    log.warning("Revoking GitHub token returned HTTP %s", resp.status)
        except NETWORK_ERRORS:
            log.exception("Failed to revoke GitHub token")

    async def _github(self, method: str, path: str, token: str, **kwargs) -> tuple[int, dict]:
        headers = {**GITHUB_HEADERS, "Authorization": f"Bearer {token}"}
        async with self.http.request(method, GITHUB_API + path, headers=headers, **kwargs) as resp:
            try:
                data = await resp.json(content_type=None)
            except ValueError:
                data = None
            return resp.status, data if isinstance(data, dict) else {}

    # ---------- Helpers ----------

    @staticmethod
    def _can_manage(member: discord.Member, thread: discord.Thread) -> bool:
        return thread.owner_id == member.id or thread.permissions_for(member).manage_threads

    async def _require_thread(self, interaction: discord.Interaction) -> discord.Thread | None:
        if not isinstance(interaction.channel, discord.Thread) or not isinstance(interaction.user, discord.Member):
            await interaction.response.send_message("Run this inside a thread.", ephemeral=True)
            return None
        return interaction.channel

    async def _remove_link(self, link: GitHubLink, reason: str) -> None:
        # Deleting the Discord webhook is what actually revokes the URL GitHub holds.
        try:
            webhook = await self.bot.fetch_webhook(link.webhook_id)
            await webhook.delete(reason=reason)
        except discord.NotFound:
            pass
        except discord.HTTPException:
            log.exception("Failed to delete webhook %s for %s", link.webhook_id, link.repo)
        await self.db.remove_github_link(link.webhook_id)
        log.info("Removed GitHub link %s -> thread %s (%s)", link.repo, link.thread_id, reason)

    async def _create_link(
        self, member: discord.Member, thread: discord.Thread, repo: str, token: str
    ) -> str:
        """Returns the linked repo's canonical name, or raises GitHubAuthError with a user-facing message."""
        status, data = await self._github("GET", f"/repos/{repo}", token)
        if status == 404:
            raise GitHubAuthError(
                f"Couldn't find **{repo}**. Check the name. If the repo belongs to an organization, "
                "an org owner may need to approve this bot's GitHub app first."
            )
        if status != 200:
            raise GitHubAuthError(f"GitHub returned an error looking up **{repo}** (HTTP {status}).")
        if not data.get("permissions", {}).get("admin"):
            raise GitHubAuthError(f"You need admin access to **{repo}** to add a webhook to it.")
        repo = data.get("full_name", repo)

        if await self.db.get_github_link(thread.id, repo) is not None:
            raise GitHubAuthError(f"**{repo}** is already linked to this thread.")

        parent = thread.parent
        if not isinstance(parent, (discord.TextChannel, discord.ForumChannel)):
            raise GitHubAuthError("This thread's channel doesn't support webhooks.")
        try:
            webhook = await parent.create_webhook(name="GitHub", reason=f"GitHub link for {repo} by {member}")
        except discord.Forbidden:
            raise GitHubAuthError(f"I need the **Manage Webhooks** permission in {parent.mention}.")
        except discord.HTTPException as e:
            if e.code == MAX_WEBHOOKS_REACHED:
                raise GitHubAuthError(
                    f"{parent.mention} already has Discord's maximum of 15 webhooks. Ask someone to "
                    "`/github unlink` a repo they no longer use, or start your thread in another channel."
                )
            raise

        # The URL goes straight from here to GitHub; it's never shown to anyone.
        url = f"{webhook.url}/github?thread_id={thread.id}"
        try:
            status, data = await self._github(
                "POST",
                f"/repos/{repo}/hooks",
                token,
                json={"name": "web", "active": True, "events": ["push"], "config": {"url": url, "content_type": "json"}},
            )
        except BaseException:
            await webhook.delete(reason="GitHub link failed")
            raise
        if status != 201:
            await webhook.delete(reason="GitHub link failed")
            detail = data.get("message") or f"HTTP {status}"
            raise GitHubAuthError(f"GitHub wouldn't add the webhook to **{repo}**: {detail}")

        await self.db.add_github_link(webhook.id, thread.guild.id, thread.id, repo, member.id, int(time.time()))
        log.info("Linked %s -> thread %s in guild %s", repo, thread.id, thread.guild.id)
        return repo

    # ---------- Commands ----------

    @github_group.command(name="link", description="Post a GitHub repo's commits in this thread.")
    @app_commands.describe(repo="The repo, as owner/name or its GitHub URL. You need admin access to it.")
    async def link(self, interaction: discord.Interaction, repo: str) -> None:
        thread = await self._require_thread(interaction)
        if thread is None:
            return
        member = interaction.user
        assert isinstance(member, discord.Member)

        if not self.client_id:
            await interaction.response.send_message("GitHub linking isn't set up on this bot.", ephemeral=True)
            return
        if not self._can_manage(member, thread):
            await interaction.response.send_message(
                "Only the thread's creator or someone with Manage Threads can link a repo here.", ephemeral=True
            )
            return
        full_name = parse_repo(repo)
        if full_name is None:
            await interaction.response.send_message(
                "That doesn't look like a repo. Use `owner/name` or a GitHub URL.", ephemeral=True
            )
            return
        if await self.db.get_github_link(thread.id, full_name) is not None:
            await interaction.response.send_message(f"**{full_name}** is already linked here.", ephemeral=True)
            return
        if len(await self.db.github_links_for_thread(thread.id)) >= MAX_LINKS_PER_THREAD:
            await interaction.response.send_message(
                f"A thread can have at most {MAX_LINKS_PER_THREAD} linked repos.", ephemeral=True
            )
            return
        if member.id in self._signing_in:
            await interaction.response.send_message(
                "Finish (or let expire) your other GitHub sign-in first.", ephemeral=True
            )
            return

        self._signing_in.add(member.id)
        try:
            await interaction.response.defer(ephemeral=True, thinking=True)
            try:
                flow = await self._start_device_flow()
            except (GitHubAuthError, *NETWORK_ERRORS) as e:
                log.warning("Starting GitHub device flow failed: %s", e)
                await interaction.followup.send("Couldn't reach GitHub. Try again in a bit.", ephemeral=True)
                return

            await interaction.followup.send(
                f"To link **{full_name}**, sign in to GitHub:\n"
                f"1. Open {flow['verification_uri']}\n"
                f"2. Enter the code **`{flow['user_code']}`**\n"
                "3. Approve access.\n\n"
                "The bot uses that access once to add a commit webhook to the repo, then discards it. "
                "The code expires in about 15 minutes.",
                ephemeral=True,
            )

            try:
                token = await self._wait_for_token(flow)
            except GitHubAuthError as e:
                await interaction.followup.send(str(e), ephemeral=True)
                return
            except NETWORK_ERRORS:
                log.exception("Polling GitHub for a token failed")
                await interaction.followup.send("Lost contact with GitHub during sign-in. Try again.", ephemeral=True)
                return

            try:
                linked = await self._create_link(member, thread, full_name, token)
            except GitHubAuthError as e:
                await interaction.followup.send(str(e), ephemeral=True)
                return
            except (*NETWORK_ERRORS, discord.HTTPException):
                log.exception("Linking %s to thread %s failed", full_name, thread.id)
                await interaction.followup.send("Something went wrong adding the webhook. Try again.", ephemeral=True)
                return
            finally:
                await self._revoke_token(token)
                del token

            await interaction.followup.send("Done.", ephemeral=True)
            await thread.send(
                f"{member.mention} linked **[{linked}](<https://github.com/{linked}>)**. "
                "New commits will be posted here.",
                allowed_mentions=discord.AllowedMentions.none(),
            )
        finally:
            self._signing_in.discard(member.id)

    @github_group.command(name="unlink", description="Stop posting a repo's commits in this thread.")
    @app_commands.describe(repo="The linked repo to remove.")
    async def unlink(self, interaction: discord.Interaction, repo: str) -> None:
        thread = await self._require_thread(interaction)
        if thread is None:
            return
        member = interaction.user
        assert isinstance(member, discord.Member)

        link = await self.db.get_github_link(thread.id, parse_repo(repo) or repo)
        if link is None:
            await interaction.response.send_message(f"**{repo}** isn't linked to this thread.", ephemeral=True)
            return
        if link.linked_by != member.id and not self._can_manage(member, thread):
            await interaction.response.send_message(
                "Only whoever linked it, the thread's creator, or someone with Manage Threads can unlink it.",
                ephemeral=True,
            )
            return

        await interaction.response.defer()
        await self._remove_link(link, f"Unlinked by {member}")
        await interaction.followup.send(
            f"{member.mention} unlinked **{link.repo}**. A repo admin can also delete the leftover "
            f"entry under <https://github.com/{link.repo}/settings/hooks>.",
            allowed_mentions=discord.AllowedMentions.none(),
        )

    @unlink.autocomplete("repo")
    async def unlink_autocomplete(self, interaction: discord.Interaction, current: str) -> list[app_commands.Choice[str]]:
        if not isinstance(interaction.channel, discord.Thread):
            return []
        links = await self.db.github_links_for_thread(interaction.channel.id)
        return [
            app_commands.Choice(name=link.repo, value=link.repo)
            for link in links
            if current.lower() in link.repo.lower()
        ][:25]

    @github_group.command(
        name="list", description="Show repos linked to this thread (or, for admins outside a thread, the whole server)."
    )
    async def list_links(self, interaction: discord.Interaction) -> None:
        assert interaction.guild is not None and isinstance(interaction.user, discord.Member)
        if isinstance(interaction.channel, discord.Thread):
            links = await self.db.github_links_for_thread(interaction.channel.id)
            show_thread = False
        elif interaction.user.guild_permissions.manage_guild:
            links = await self.db.github_links_for_guild(interaction.guild.id)
            show_thread = True
        else:
            await interaction.response.send_message(
                "Run this inside a thread to see its linked repos.", ephemeral=True
            )
            return

        if not links:
            await interaction.response.send_message("No linked repos.", ephemeral=True)
            return

        lines = []
        for link in links:
            line = f"**{link.repo}**"
            if show_thread:
                line += f" in <#{link.thread_id}>"
            line += f" · linked by <@{link.linked_by}> · last activity <t:{link.last_activity}:R>"
            if link.warned_at is not None:
                line += " · ⚠️ pending removal"
            lines.append(line)

        text = "\n".join(lines)
        if len(text) > 1900:
            text = text[:1900].rsplit("\n", 1)[0] + "\n…"
        await interaction.response.send_message(text, ephemeral=True)

    @github_group.command(name="keep", description="Reset the inactivity timer for this thread's linked repos.")
    async def keep(self, interaction: discord.Interaction) -> None:
        thread = await self._require_thread(interaction)
        if thread is None:
            return
        if not await self.db.github_links_for_thread(thread.id):
            await interaction.response.send_message("No repos are linked to this thread.", ephemeral=True)
            return
        await self.db.touch_github_links_for_thread(thread.id, int(time.time()))
        await interaction.response.send_message(
            f"Kept. Links here won't be flagged again for {INACTIVE_AFTER // 86400} days without commits.",
            ephemeral=True,
        )

    # ---------- Activity tracking and cleanup ----------

    @commands.Cog.listener()
    async def on_message(self, message: discord.Message) -> None:
        if message.webhook_id is not None and isinstance(message.channel, discord.Thread):
            await self.db.touch_github_link(message.webhook_id, int(time.time()))

    @commands.Cog.listener()
    async def on_raw_thread_delete(self, payload: discord.RawThreadDeleteEvent) -> None:
        for link in await self.db.github_links_for_thread(payload.thread_id):
            await self._remove_link(link, "Thread deleted")

    @tasks.loop(hours=6)
    async def cleanup(self) -> None:
        now = int(time.time())
        for link in await self.db.all_github_links():
            guild = self.bot.get_guild(link.guild_id)
            if guild is None:
                continue  # Temporarily unavailable; on_guild_remove handles real removals.

            thread = guild.get_thread(link.thread_id)
            if thread is None:
                try:
                    thread = await self.bot.fetch_channel(link.thread_id)
                except discord.NotFound:
                    await self._remove_link(link, "Thread no longer exists")
                    continue
                except discord.HTTPException:
                    continue

            if link.warned_at is not None and now - link.warned_at >= GRACE_PERIOD:
                await self._remove_link(link, "Inactive")
                await self._notify(
                    thread,
                    f"Unlinked **{link.repo}** after {(INACTIVE_AFTER + GRACE_PERIOD) // 86400} days without "
                    "commits. Use `/github link` to add it again.",
                )
            elif link.warned_at is None and now - link.last_activity >= INACTIVE_AFTER:
                await self.db.set_github_link_warned(link.webhook_id, now)
                await self._notify(
                    thread,
                    f"No commits from **{link.repo}** in {INACTIVE_AFTER // 86400} days. It'll be unlinked in "
                    f"{GRACE_PERIOD // 86400} days unless someone pushes a commit or runs `/github keep`.",
                )

    @cleanup.before_loop
    async def before_cleanup(self) -> None:
        await self.bot.wait_until_ready()

    async def _notify(self, thread, text: str) -> None:
        if not isinstance(thread, discord.Thread):
            return
        try:
            await thread.send(text)
        except discord.HTTPException:
            log.warning("Couldn't post cleanup notice in thread %s", thread.id)


async def setup(bot: commands.Bot) -> None:
    db: Database = bot.db  # type: ignore[attr-defined]
    await bot.add_cog(GitHubLinks(bot, db))
