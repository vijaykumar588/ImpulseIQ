"""
Pluggable OHLCV data layer.

TradingView has no public data API, so the default implementation here uses
Yahoo Finance via the `yfinance` package. To use a different provider
(Alpaca, Polygon.io, IBKR, a broker feed, etc.), implement `DataSource` and
register it in `get_data_source()`.
"""

from abc import ABC, abstractmethod
import pandas as pd


class DataSource(ABC):
    @abstractmethod
    def get_ohlcv(self, ticker: str, interval: str, lookback: int) -> pd.DataFrame:
        """
        Return a DataFrame indexed by datetime with columns:
        ['open', 'high', 'low', 'close', 'volume']
        `lookback` is the number of bars requested (not calendar days).
        """
        raise NotImplementedError


class YFinanceSource(DataSource):
    _INTERVAL_TO_PERIOD = {
        "1m": "7d", "5m": "60d", "15m": "60d", "1h": "730d",
        "1d": "5y", "1wk": "10y",
    }

    def get_ohlcv(self, ticker: str, interval: str, lookback: int) -> pd.DataFrame:
        import yfinance as yf

        period = self._INTERVAL_TO_PERIOD.get(interval, "5y")
        df = yf.download(
            ticker, period=period, interval=interval,
            auto_adjust=True, progress=False,
        )
        if df.empty:
            raise ValueError(f"No data returned for ticker '{ticker}'")

        if isinstance(df.columns, pd.MultiIndex):
            df.columns = [c[0].lower() for c in df.columns]
        else:
            df.columns = [c.lower() for c in df.columns]

        df = df.rename(columns={"adj close": "close"})
        keep = [c for c in ["open", "high", "low", "close", "volume"] if c in df.columns]
        df = df[keep].dropna()

        if lookback and len(df) > lookback:
            df = df.iloc[-lookback:]
        return df


_REGISTRY = {
    "yfinance": YFinanceSource,
}


def get_data_source(name: str = "yfinance") -> DataSource:
    if name not in _REGISTRY:
        raise KeyError(
            f"Unknown data source '{name}'. Registered: {list(_REGISTRY)}. "
            "Implement DataSource and add it to _REGISTRY to use another provider."
        )
    return _REGISTRY[name]()
