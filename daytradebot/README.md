# Discord Day-Trading Bot (Paper) — Alpaca WebSocket + 4 Bullish Strategies

Commands:
- `!start SYMBOL` — start analyzing a ticker (e.g., `!start TQQQ`)
- `!stop` — stop analyzing
- `!status` — show status (running, symbol, R/day)
- `!risk 0.01` — set 1% risk per trade
- `!tp 2` — set take-profit at +2R
- `!sl 0.02` — set immediate-loss cutoff at 2%

## Setup
1. Create a Discord bot, invite it to your server (Message Content intent enabled).
2. `cp .env.example .env` and fill `DISCORD_TOKEN`, `ALPACA_API_KEY_ID`, `ALPACA_API_SECRET_KEY`.
3. Set your channel ID in `config.yaml` → `discord_channel_id`.
4. Install & run:
```bash
pip install -r requirements.txt
python bot.py