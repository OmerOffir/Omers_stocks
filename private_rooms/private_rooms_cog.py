# private_rooms_cog.py
from __future__ import annotations

import os
import re
import unicodedata
import asyncio
from datetime import datetime, timedelta, timezone
from typing import Optional, Dict, Set, Tuple

import discord
from discord.ext import commands, tasks
from discord import app_commands

# -------- Config via ENV --------
CATEGORY_NAME = os.getenv("PRIVATE_ROOMS_CATEGORY", "Private Rooms")
STAFF_ROLE_IDS = [int(x) for x in os.getenv("PRIVATE_ROOMS_STAFF_ROLE_IDS", "").split(",") if x.strip().isdigit()]
CAPACITY = int(os.getenv("PRIVATE_ROOMS_CAPACITY", "450"))

TTL_MIN = int(os.getenv("PRIVATE_ROOMS_TTL_MINUTES", "60"))                 # time-to-live (hard limit)
INACTIVITY_MIN = int(os.getenv("PRIVATE_ROOMS_INACTIVITY_MINUTES", "60"))   # inactivity window
SWEEP_INTERVAL_MIN = int(os.getenv("PRIVATE_ROOMS_SWEEP_INTERVAL_MINUTES", "5"))
WELCOME_GRACE_MIN = int(os.getenv("PRIVATE_ROOMS_WELCOME_GRACE_MINUTES", "3"))

TOPIC_PREFIX_OWNER = "OWNER:"
TOPIC_PREFIX_EXPIRES = "EXPIRES:"   # epoch seconds UTC
ROOM_SUFFIX = "privateroom"

def now_utc() -> datetime:
    return datetime.now(tz=timezone.utc)

# ---------- Helpers ----------
def slugify_username(name: str) -> str:
    name = unicodedata.normalize("NFKD", name)
    name = name.encode("ascii", "ignore").decode("ascii", "ignore").lower()
    name = re.sub(r"[^a-z0-9_-]+", "-", name).strip("-")
    name = re.sub(r"-{2,}", "-", name)
    if not name:
        name = "user"
    return name[:80]

def build_room_name_for(member: discord.Member, existing_names: Optional[Set[str]] = None) -> str:
    base = f"{slugify_username(member.name)}_{ROOM_SUFFIX}"
    name = base
    if existing_names is None:
        return name[:100]
    i = 2
    while name in existing_names or len(name) > 100:
        candidate = f"{base}-{i}"
        if len(candidate) <= 100:
            name = candidate
            i += 1
        else:
            base = base[: max(0, 100 - len(f"-{i}"))]
            name = f"{base}-{i}"
            i += 1
    return name

def make_topic(owner_id: int, expires_epoch: int) -> str:
    return f"{TOPIC_PREFIX_OWNER}{owner_id};{TOPIC_PREFIX_EXPIRES}{expires_epoch}"

def parse_topic(topic: Optional[str]) -> Tuple[Optional[int], Optional[int]]:
    """
    Returns (owner_id, expires_epoch) if present, else (None, None).
    Expected format: "OWNER:<id>;EXPIRES:<epoch>"
    """
    if not topic:
        return None, None
    parts = [p.strip() for p in topic.split(";") if p.strip()]
    owner_id = None
    expires = None
    for p in parts:
        if p.upper().startswith(TOPIC_PREFIX_OWNER):
            try:
                owner_id = int(p.split(":", 1)[1].strip())
            except Exception:
                pass
        elif p.upper().startswith(TOPIC_PREFIX_EXPIRES):
            try:
                expires = int(p.split(":", 1)[1].strip())
            except Exception:
                pass
    return owner_id, expires

async def get_last_activity_at(ch: discord.TextChannel) -> datetime:
    """Return last activity timestamp in UTC: last message time, else creation time."""
    if ch.last_message_id:
        try:
            msg = await ch.fetch_message(ch.last_message_id)
            return msg.created_at.replace(tzinfo=timezone.utc)
        except Exception:
            pass
    try:
        async for msg in ch.history(limit=1, oldest_first=False):
            return msg.created_at.replace(tzinfo=timezone.utc)
    except Exception:
        pass
    return ch.created_at.replace(tzinfo=timezone.utc)


class PrivateRooms(commands.Cog):
    def __init__(self, bot: commands.Bot):
        self.bot = bot
        # periodic prune loop
        self._prune_loop.change_interval(minutes=SWEEP_INTERVAL_MIN)
        self._prune_loop.start()

    def cog_unload(self):
        try:
            self._prune_loop.cancel()
        except Exception:
            pass

    # ---------- Utilities ----------
    async def _ensure_category(self, guild: discord.Guild) -> discord.CategoryChannel:
        cat = discord.utils.get(guild.categories, name=CATEGORY_NAME)
        if cat is None:
            cat = await guild.create_category(CATEGORY_NAME, reason="Create private rooms category")
        return cat

    def _build_overwrites(
        self,
        guild: discord.Guild,
        owner: discord.Member
    ) -> Dict[discord.abc.Snowflake, discord.PermissionOverwrite]:
        overwrites: Dict[discord.abc.Snowflake, discord.PermissionOverwrite] = {
            guild.default_role: discord.PermissionOverwrite(view_channel=False),
            owner: discord.PermissionOverwrite(view_channel=True, send_messages=True, read_message_history=True),
        }
        # Allow all bots
        for m in guild.members:
            if m.bot:
                overwrites[m] = discord.PermissionOverwrite(view_channel=True, send_messages=True, read_message_history=True)
        # Allow staff roles (optional)
        for role_id in STAFF_ROLE_IDS:
            role = guild.get_role(role_id)
            if role:
                overwrites[role] = discord.PermissionOverwrite(
                    view_channel=True,
                    send_messages=True,
                    read_message_history=True,
                    manage_messages=True
                )
        return overwrites

    async def _find_owner_room(self, guild: discord.Guild, member_id: int) -> Optional[discord.TextChannel]:
        category = discord.utils.get(guild.categories, name=CATEGORY_NAME)
        if not category:
            return None
        for ch in category.channels:
            if not isinstance(ch, discord.TextChannel):
                continue
            owner_id, _ = parse_topic(ch.topic or "")
            if owner_id == member_id:
                return ch
        return None

    async def _count_open_rooms(self, guild: discord.Guild) -> int:
        category = discord.utils.get(guild.categories, name=CATEGORY_NAME)
        if not category:
            return 0
        cnt = 0
        for ch in category.channels:
            if isinstance(ch, discord.TextChannel):
                owner_id, _ = parse_topic(ch.topic or "")
                if owner_id:
                    cnt += 1
        return cnt

    # ---------- Core ops ----------
    async def _create_room(self, member: discord.Member) -> Optional[discord.TextChannel]:
        guild = member.guild
        category = await self._ensure_category(guild)

        # Capacity guard
        open_count = await self._count_open_rooms(guild)
        if open_count >= CAPACITY:
            return None

        existing_names = {ch.name for ch in category.channels if isinstance(ch, discord.TextChannel)}
        ch_name = build_room_name_for(member, existing_names)

        expires_at = now_utc() + timedelta(minutes=TTL_MIN)
        topic = make_topic(owner_id=member.id, expires_epoch=int(expires_at.timestamp()))

        overwrites = self._build_overwrites(guild, member)

        channel = await guild.create_text_channel(
            name=ch_name,
            category=category,
            overwrites=overwrites,
            topic=topic,
            reason=f"Create on-demand private room for {member} ({member.id})",
        )
        try:
            await channel.send(
                f"Hi {member.mention}! This is your on-demand private room.\n"
                f"- TTL: {TTL_MIN} minutes (auto-closes)\n"
                f"- Inactivity timeout: {INACTIVITY_MIN} minutes\n"
                f"You can reopen any time with `/myroom open`."
            )
        except Exception:
            pass
        return channel

    async def _delete_room(self, ch: discord.TextChannel, reason: str) -> None:
        try:
            await ch.delete(reason=reason)
        except Exception:
            pass

    # ---------- Auto-prune loop ----------
    @tasks.loop(minutes=5)
    async def _prune_loop(self):
        now = now_utc()
        welcome_grace_cutoff = now - timedelta(minutes=WELCOME_GRACE_MIN)
        inactivity_cutoff = now - timedelta(minutes=INACTIVITY_MIN)

        for guild in list(self.bot.guilds):
            category = discord.utils.get(guild.categories, name=CATEGORY_NAME)
            if not category:
                continue
            for ch in list(category.channels):
                if not isinstance(ch, discord.TextChannel):
                    continue
                topic = ch.topic or ""
                owner_id, expires_epoch = parse_topic(topic)
                if not owner_id:
                    continue  # unmanaged

                # Grace: skip very new channels
                if ch.created_at.replace(tzinfo=timezone.utc) > welcome_grace_cutoff:
                    continue

                # Hard TTL check
                if expires_epoch:
                    expires_at = datetime.fromtimestamp(expires_epoch, tz=timezone.utc)
                    if now >= expires_at:
                        await self._delete_room(ch, "TTL expired (on-demand private room)")
                        await asyncio.sleep(0.2)
                        continue

                # Inactivity check
                try:
                    last_activity = await get_last_activity_at(ch)
                    if last_activity <= inactivity_cutoff:
                        await self._delete_room(ch, f"No activity for {INACTIVITY_MIN} minutes (on-demand private room)")
                        await asyncio.sleep(0.2)
                except Exception:
                    # If cannot read history, skip
                    await asyncio.sleep(0.1)
                    continue

    @_prune_loop.before_loop
    async def _before_prune(self):
        await self.bot.wait_until_ready()

    # ---------- Slash Commands ----------
    room_group = app_commands.Group(name="myroom", description="Your on-demand private room")

    @room_group.command(name="open", description="Open your private room (on-demand, temporary).")
    async def open_room(self, interaction: discord.Interaction):
        await interaction.response.defer(ephemeral=True)
        guild = interaction.guild
        user = interaction.user
        if not guild or not isinstance(user, discord.Member):
            return await interaction.followup.send("This command must be used in a server.", ephemeral=True)

        # If already exists → refresh TTL & return link
        existing = await self._find_owner_room(guild, user.id)
        if existing:
            # Refresh TTL (extend)
            expires_at = now_utc() + timedelta(minutes=TTL_MIN)
            new_topic = make_topic(owner_id=user.id, expires_epoch=int(expires_at.timestamp()))
            try:
                await existing.edit(topic=new_topic, reason="Refresh TTL on open")
            except Exception:
                pass
            return await interaction.followup.send(f"Your room is ready: {existing.mention}", ephemeral=True)

        # Create new, respecting capacity
        channel = await self._create_room(user)
        if channel is None:
            # capacity reached
            return await interaction.followup.send(
                "The server is currently full. Please try again later.",
                ephemeral=True
            )
        await interaction.followup.send(f"Your room is ready: {channel.mention}", ephemeral=True)

    @room_group.command(name="status", description="Get a link and expiration info for your room.")
    async def status_room(self, interaction: discord.Interaction):
        await interaction.response.defer(ephemeral=True)
        guild = interaction.guild
        user = interaction.user
        if not guild or not isinstance(user, discord.Member):
            return await interaction.followup.send("This command must be used in a server.", ephemeral=True)

        ch = await self._find_owner_room(guild, user.id)
        if not ch:
            return await interaction.followup.send("You don't have an open room. Use `/myroom open`.", ephemeral=True)

        _, exp_epoch = parse_topic(ch.topic or "")
        if exp_epoch:
            exp_at = datetime.fromtimestamp(exp_epoch, tz=timezone.utc)
            return await interaction.followup.send(
                f"Room: {ch.mention}\nExpires at (UTC): {exp_at.strftime('%Y-%m-%d %H:%M:%S')}",
                ephemeral=True
            )
        return await interaction.followup.send(f"Room: {ch.mention}\nExpires: unknown", ephemeral=True)

    @room_group.command(name="close", description="Close your private room now.")
    async def close_room(self, interaction: discord.Interaction):
        await interaction.response.defer(ephemeral=True)
        guild = interaction.guild
        user = interaction.user
        if not guild or not isinstance(user, discord.Member):
            return await interaction.followup.send("This command must be used in a server.", ephemeral=True)

        ch = await self._find_owner_room(guild, user.id)
        if not ch:
            return await interaction.followup.send("You don't have an open room.", ephemeral=True)

        await self._delete_room(ch, "Closed by user request")
        await interaction.followup.send("Your room has been closed.", ephemeral=True)


async def setup(bot: commands.Bot):
    await bot.add_cog(PrivateRooms(bot))
