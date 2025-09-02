from __future__ import annotations
import os, json, asyncio
import pandas as pd
from typing import AsyncGenerator
import websockets
#5ff31694-b279-45a6-9105-63720f01de99
from .provider_base import ProviderBase

ALPACA_FEED = "iex"
ALPACA_WS_URL = f"wss://stream.data.alpaca.markets/v2/{ALPACA_FEED}"

class AlpacaWSProvider(ProviderBase):
    """
    Streams real-time bars (1m) from Alpaca. Yields a growing DataFrame:
      columns = ['open','high','low','close','volume']  (UTC index)
    """

    def __init__(self, maxlen: int = 4000):
        self.maxlen = maxlen

    async def stream_bars(self, symbol: str) -> AsyncGenerator[pd.DataFrame, None]:
        key = "PKSGDTBVJUZC90X0QGZW"
        sec = "xNGzHtbb3RtqLPWp8ONU8iiRcJVIZFIOcATur2CT"
        if not key or not sec:
            raise RuntimeError("Missing ALPACA_API_KEY_ID / ALPACA_API_SECRET_KEY in env")

        sym = symbol.upper() 
        df = pd.DataFrame(columns=["open","high","low","close","volume"])
        df.index = pd.DatetimeIndex([], tz="UTC")

        backoff = 1
        while True:
            try:
                async with websockets.connect(
                    ALPACA_WS_URL,
                    ping_interval=15,
                    ping_timeout=15,
                    max_size=None
                ) as ws:
                    # auth
                    await ws.send(json.dumps({"action": "auth", "key": key, "secret": sec}))
                    auth_resp = await ws.recv()  # usually {"T":"success","msg":"connected"} then {"T":"success","msg":"authenticated"}
                    # subscribe
                    await ws.send(json.dumps({"action": "subscribe", "bars": [sym]}))
                    # drain initial conf messages
                    _ = await ws.recv()

                    backoff = 1  # reset backoff on success

                    while True:
                        raw = await ws.recv()
                        # Alpaca sends arrays of events
                        try:
                            data = json.loads(raw)
                        except Exception:
                            continue
                        events = data if isinstance(data, list) else [data]

                        updated = False
                        for ev in events:
                            # Bars: {"T":"b","S":"AAPL","o":...,"h":...,"l":...,"c":...,"v":...,"t":"2025-08-29T13:05:00Z"}
                            if ev.get("T") == "b" and ev.get("S") == sym:
                                ts = pd.Timestamp(ev["t"]).tz_convert("UTC") if pd.Timestamp(ev["t"]).tzinfo \
                                     else pd.Timestamp(ev["t"], tz="UTC")
                                row = pd.DataFrame(
                                    [[ev["o"], ev["h"], ev["l"], ev["c"], ev["v"]]],
                                    columns=["open","high","low","close","volume"],
                                    index=pd.DatetimeIndex([ts], tz="UTC")
                                )
                                # upsert
                                df = pd.concat([df[~df.index.isin(row.index)], row]).sort_index()
                                df = df.iloc[-self.maxlen:]
                                updated = True

                        if updated:
                            yield df

            except asyncio.CancelledError:
                raise
            except Exception:
                await asyncio.sleep(backoff)
                backoff = min(backoff * 2, 30)
