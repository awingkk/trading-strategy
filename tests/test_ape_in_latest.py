"""Test ape in algo."""

import logging
import datetime
from typing import Dict

import backtrader as bt
import pandas as pd
import pytest
from backtrader import analyzers

from capitalgram.candle import GroupedCandleUniverse
from capitalgram.exchange import ExchangeUniverse
from capitalgram.liquidity import GroupedLiquidityUniverse
from capitalgram.timebucket import TimeBucket
from capitalgram.client import Capitalgram
from capitalgram.frameworks.backtrader import prepare_candles_for_backtrader, add_dataframes_as_feeds, CapitalgramFeed
from capitalgram.pair import PandasPairUniverse


logger = logging.getLogger(__name__)


def prefilter_pairs(all_pairs_dataframe: pd.DataFrame) -> pd.DataFrame:
    """Get rid of pairs that we definitely are not interested in.

    This will greatly speed up the later computations, as we do not need to
    calculate the opening volumes for thousands of pairs.

    Note that may induce survivorship bias - we use thiws mainly
    to ensure the example strategy completes fast enough.
    """
    pairs: pd.DataFrame = all_pairs_dataframe.loc[
        (all_pairs_dataframe['buy_volume_all_time'] > 5_000_000)  # 500k min buys
    ]
    return pairs


def update_pair_liquidity_threshold(
        now_: datetime.datetime,
        threshold: float,
        reached_state: dict,
        pair_universe: PandasPairUniverse,
        liquidity_universe: GroupedLiquidityUniverse) -> dict:
    """Check which pairs reach the liquidity threshold on a given day.

    :param threshold: Available liquidity, in US dollar

    :return: Dict of pair ids who reached the liquidity threshold and how much liquidity they had
    """

    new_entries = {}

    ts = pd.Timestamp(now_)

    for pair_id in pair_universe.get_all_pair_ids():
        if pair_id not in reached_state:
            # Get the todays liquidity
            liquidity_samples = liquidity_universe.get_liquidity_by_pair(pair_id)
            # We determine the available liquidity by the daily open
            try:
                liquidity_today = liquidity_samples["open"][ts]
            except KeyError:
                liquidity_today = 0
            if liquidity_today >= threshold:
                reached_state[pair_id] = now_
                new_entries[pair_id] = liquidity_today

    return new_entries


class ApeTheLatestStrategy(bt.Strategy):
    """A strategy that picks the latest token to reach the liquidity threshold, then goes all in.

    https://www.backtrader.com/docu/quickstart/quickstart/#adding-some-logic-to-the-strategy
    """

    def __init__(self,
                 start_date,
                 exchange_universe: ExchangeUniverse,
                 pair_universe: PandasPairUniverse,
                 liquidity_universe: GroupedLiquidityUniverse,
                 min_liquidity=100_000):

        logger.info("Initializing")

        # We use timestamps, not linear arrays,
        # when looking up Pandas data
        self.start_date = start_date

        # Set up our datasets
        self.exchange_universe = exchange_universe
        self.pair_universe = pair_universe
        self.liquidity_universe = liquidity_universe

        #: A pair becomes investible when it reaches this liquidity
        self.min_liquidity = min_liquidity

        #: We operate on daily candles.
        #: At each tick, we process to the next candle
        self.day = 0

        #: paid id -> date mapping for pairs that have become
        #: liquid enough to invest
        self.liquidity_reached_state: Dict[int, datetime.datetime] = {}

        #: Map Capitalgram pair_id to Backtrader internal "pair" preseentation,
        #: because we mostly operate on raw Pandas data and not try to use
        #: Backtrader facilities for our custom data formats like liquidity
        self.backtrader_pair_map: Dict[int, CapitalgramFeed] = {}
        pair: CapitalgramFeed
        for pair in self.datas:
            pair_info = pair.pair_info
            self.backtrader_pair_map[pair_info.pair_id] = pair

    def next(self):
        """Tick the strategy.

        Because we are using daily candles, tick will run once per each day.
        """

        # Advance to the next day
        self.day += 1
        today = self.start_date + datetime.timedelta(days=self.day)

        logger.info("Starting day %d, we have %d pairs that have reached the liquidity threshold",
                    self.day,
                    len(self.liquidity_reached_state))

        # Refresh which cross the liquidity threshold today
        new_entries = update_pair_liquidity_threshold(
            today,
            self.min_liquidity,
            self.liquidity_reached_state,
            self.pair_universe,
            self.liquidity_universe
        )

        # Diplay any new pairs that crossed the liquidity threshold and entered the markets
        for pair_id, available_liquidity in new_entries.items():
            pair_info = self.pair_universe.get_pair_by_id(pair_id)
            name = pair_info.get_friendly_name(self.exchange_universe)
            logger.info("Pair %s (%d - %s) reached liquidity %f on %s", name, pair_id, pair_info.address, available_liquidity, today)

        # Sort pairs by the latest new
        fresh_pairs = [(pair_id, liquidity_reached_at) for pair_id, liquidity_reached_at in self.liquidity_reached_state.items()]
        fresh_pairs = sorted(fresh_pairs, key=lambda x: x[1], reverse=True)
        latest_pair_id = fresh_pairs[0][0]

        latest_pair = self.pair_universe.get_pair_by_id(latest_pair_id)
        latest_pair_name = latest_pair.get_friendly_name(self.exchange_universe)
        logger.info("Latest pair today is %s", latest_pair_name)

        backtrader_pair = self.backtrader_pair_map[latest_pair_id]
        if self.getposition(backtrader_pair).size > 0:
            logger.info("Already owning %s", latest_pair_name)
        else:
            self.close(backtrader_pair)
            cash = self.broker.get_cash()
            price = backtrader_pair.close[0]
            assert price > 0
            size = cash / price
            logger.info("Buying into %s, position size %f, available cash %f", latest_pair_name, size, cash)
            self.buy(backtrader_pair, size=size, exectype=bt.Order.Market)


@pytest.mark.skip(reason="Not finished yet")
def test_backtrader_ape_in_strategy(logger, persistent_test_client: Capitalgram):
    """Ape in to the latest token every day."""

    client = persistent_test_client

    exchange_universe = client.fetch_exchange_universe()

    # Decompress the pair dataset to Python map
    columnar_pair_table = client.fetch_pair_universe()

    # Make our universe 40x smaller and faster to compute
    filtered_pairs = prefilter_pairs(columnar_pair_table.to_pandas())

    # We limit candles to a specific date range to make this notebook deterministic
    start = datetime.datetime(2020, 10, 1)
    end = datetime.datetime(2021, 6, 1)

    # Make the trading pair data easily accessible
    pair_universe = PandasPairUniverse(filtered_pairs)
    wanted_pair_ids = pair_universe.get_all_pair_ids()

    # Get daily candles as Pandas DataFrame
    all_candles = client.fetch_all_candles(TimeBucket.d1).to_pandas()
    all_candles = all_candles.loc[
        all_candles["pair_id"].isin(wanted_pair_ids) &
        (all_candles["timestamp"] >= start),  # Backtrader assumes all price feeds have the same linear index
    ]
    candle_universe = GroupedCandleUniverse(prepare_candles_for_backtrader(all_candles))

    all_liquidity = client.fetch_all_liquidity_samples(TimeBucket.d1).to_pandas()
    all_liquidity = all_liquidity.loc[all_liquidity["pair_id"].isin(wanted_pair_ids)]
    liquidity_universe = GroupedLiquidityUniverse(all_liquidity)

    logger.info("Starting the strategy. We have %d pairs, %d candles, %d liquidity samples",
                pair_universe.get_count(),
                candle_universe.get_candle_count(),
                liquidity_universe.get_sample_count())

    # Create a cerebro entity
    cerebro = bt.Cerebro(stdstats=False)

    # Add a strategy
    cerebro.addstrategy(ApeTheLatestStrategy,
                        start_date=start,
                        exchange_universe=exchange_universe,
                        pair_universe=pair_universe,
                        liquidity_universe=liquidity_universe)

    logger.info("Preparing feeds")
    # Pass all Sushi pairs to the data fees to the strategy
    # noinspection JupyterKernel
    feeds = [df for pair_id, df in candle_universe.get_all_pairs()]
    add_dataframes_as_feeds(
        cerebro,
        pair_universe,
        feeds,
        start,
        end,
        TimeBucket.d1)
    logger.info("All feeds ready")

    # Anaylyse won vs. loss of trades
    cerebro.addanalyzer(analyzers.TradeAnalyzer, _name="tradeanalyzer")  # trade analyzer

    results = cerebro.run()

    strategy: ApeTheLatestStrategy = results[0]
