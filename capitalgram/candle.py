import enum
import typing
from dataclasses import dataclass, fields
from typing import List

import numpy
import pandas as pd
from dataclasses_json import dataclass_json

from capitalgram.caip import ChainAddressTuple
from capitalgram.chain import ChainId
from capitalgram.units import NonChecksummedAddress, UNIXTimestamp, USDollarAmount, BlockNumber


class CandleBucket(enum.Enum):
    """Available time windows for candle generation..

    All candles are upsampled from 1m data.
    """

    m1 = "1m"
    m5 = "5m"
    m15 = "15m"
    h1 = "1h"
    h4 = "4h"
    h24 = "24h"
    d7 = "7d"
    d30 = "30d"


@dataclass_json
@dataclass
class Candle:
    """DEX trade candle.

    OHLCV candle with some extra information available,
    as our chain analysis allows deeper visibility that you would
    get with traditional exchanges.
    """
    chain_id: ChainId  # 1 for Ethereum
    address: NonChecksummedAddress  # Pair contract address

    #: Open timestamp for this candle.
    #: Note that the close timestamp you need to supply yourself based on the context.
    timestamp: UNIXTimestamp  # UNIX timestamp as seconds

    #: USD exchange rate of the quote token used to
    #: convert to dollar amounts in this candle.
    #: Note that currently any USD stablecoin is
    #: assumed to be 1:1 and the candle server cannot
    #: handle exchange rate difference among stablecoins.
    exchange_rate: float

    #: OHLCV core data
    open: USDollarAmount
    close: USDollarAmount
    high: USDollarAmount
    low: USDollarAmount

    #: Number of buys happened during the candle period
    buys: int

    #: Number of sells happened during the candle period
    sells: int

    #: Volume data.
    #: Note that we separate buys and sells
    buy_volume: USDollarAmount
    sell_volume: USDollarAmount

    #: Average trade size
    avg: USDollarAmount

    #: Blockchain tracking information
    start_block: BlockNumber
    end_block: BlockNumber

    def __repr__(self):
        return f"@{self.timestamp} O:{self.open} H:{self.high} L:{self.low} C:{self.close} V:{self.volume} B:{self.buys} S:{self.sells} SB:{self.start_block} EB:{self.end_block}"

    @property
    def caip(self) -> ChainAddressTuple:
        """Unique identifier for the trading pair"""
        return ChainAddressTuple(self.chain_id.value, self.address)

    @property
    def trades(self) -> int:
        """Amount of all trades during the candle period."""
        return self.buys + self.sells

    @property
    def volume(self) -> USDollarAmount:
        """Total volume during the candle period.

        Unline in traditional CEX trading, we can separate buy volume and sell volume from each other,
        becauase liquidity provider is a special role.
        """
        return self.buy_volume + self.sell_volume

    @classmethod
    def to_dataframe(cls) -> pd.DataFrame:
        """Return emptry Pandas dataframe presenting candle data."""

        # https://stackoverflow.com/a/51953411/315168
        _fields = {field.name: field.type for field in fields(cls)}

        resolved_hints = typing.get_type_hints(cls)
        field_names = [field.name for field in fields(cls)]
        resolved_field_types = {name: resolved_hints[name] for name in field_names}

        df = pd.DataFrame(index=None)
        for name, fdesc in resolved_field_types.items():
            if name == "timestamp":
                pf = "datetime64[s]"
            elif name == "chain_id":
                # https://stackoverflow.com/a/29503414/315168
                # pf = pd.Categorical([str(f.value) for f in ChainId])
                # Setting up categories much pain...
                # Pandas API such horrible
                pf = "int"
            elif fdesc == int:
                pf = "int"
            elif fdesc == float:
                pf = "float"
            elif fdesc == str:
                # Address
                pf = "string_"
            else:
                raise RuntimeError(f"Cannot handle {name}: {fdesc}")

            df[name] = pd.Series(dtype=pf)

        return df


@dataclass_json
@dataclass
class CandleResult:
    """Server-reply for live queried candle data."""

    #: A bunch of candles.
    #: Candles are unordered and subject to client side sorting.
    #: Multiple pairs and chains may be present in candles.
    candles: List[Candle]

    def sort_by_timestamp(self):
        """In-place sorting of candles by their timestamp."""
        self.candles.sort(key=lambda c: c.timestamp)


