# crypto/crypto_cog.py
import os
import asyncio
import discord
from discord.ext import commands, tasks

from crypto.crypto_bot import get_prices, build_coin_embed

CRYPTO_CHANNEL_ID = int("1418555720160247958") 
CRYPTO_POST_EVERY_MIN = int(os.getenv("POST_EVERY_MIN", "30")) 

class CryptoPricesCog(commands.Cog):
    def __init__(self, bot: commands.Bot):
        self.bot = bot
        # set interval and start background loop
        self.auto_post_loop.change_interval(minutes=CRYPTO_POST_EVERY_MIN)
        self.auto_post_loop.start()

    def cog_unload(self):
        self.auto_post_loop.cancel()

    @tasks.loop(minutes=60)
    async def auto_post_loop(self):
        if not CRYPTO_CHANNEL_ID:
            return
        ch = self.bot.get_channel(CRYPTO_CHANNEL_ID)
        if ch is None:
            try:
                ch = await self.bot.fetch_channel(CRYPTO_CHANNEL_ID)
            except Exception:
                return
        data = await get_prices(("BTC-USD", "ETH-USD"))
        btc = build_coin_embed("BTC-USD", data["BTC-USD"]["price"], data["BTC-USD"]["change_pct"])
        eth = build_coin_embed("ETH-USD", data["ETH-USD"]["price"], data["ETH-USD"]["change_pct"])
        await ch.send(embeds=[btc, eth])

    @auto_post_loop.before_loop
    async def _before(self):
        await self.bot.wait_until_ready()
        await asyncio.sleep(2)

    # on-demand triggers in the same channel
    @commands.Cog.listener()
    async def on_message(self, message: discord.Message):
        if message.author.bot:
            return
        if not CRYPTO_CHANNEL_ID or message.channel.id != CRYPTO_CHANNEL_ID:
            return
        if message.channel.id not in [1418555720160247958]:
            return
        
        content = (message.content or "").strip().lower()
        if content in {"?", "?crypto"}:
            data = await get_prices(("BTC-USD", "ETH-USD"))
            btc = build_coin_embed("BTC-USD", data["BTC-USD"]["price"], data["BTC-USD"]["change_pct"])
            eth = build_coin_embed("ETH-USD", data["ETH-USD"]["price"], data["ETH-USD"]["change_pct"])
            async with message.channel.typing():
                await message.channel.send(embeds=[btc, eth])
        elif content in {"?btc", "?bitcoin"}:
            d = await get_prices(("BTC-USD",))
            await message.channel.send(embeds=[build_coin_embed("BTC-USD", d["BTC-USD"]["price"], d["BTC-USD"]["change_pct"])])
        elif content in {"?eth", "?ethereum"}:
            d = await get_prices(("ETH-USD",))
            await message.channel.send(embeds=[build_coin_embed("ETH-USD", d["ETH-USD"]["price"], d["ETH-USD"]["change_pct"])])

async def setup(bot: commands.Bot):
    await bot.add_cog(CryptoPricesCog(bot))
