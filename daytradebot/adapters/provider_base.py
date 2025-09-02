from __future__ import annotations
import abc
import pandas as pd

class ProviderBase(abc.ABC):
    @abc.abstractmethod
    async def stream_bars(self, symbol: str):
        """Async generator -> pandas DataFrame with index tz-aware UTC and
        columns ['open','high','low','close','volume']."""
        yield pd.DataFrame()

    def reset_heartbeats(self):
        pass

    async def maybe_heartbeat(self, channel):
        return
