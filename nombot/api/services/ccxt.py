"""CCXT API Facade"""

import asyncio
from itertools import product
from dataclasses import dataclass

import ccxt.async as ccxt
from ccxt.base.errors import ExchangeNotAvailable, ExchangeError

from bors.app.log import LoggerMixin

from nombot.generics.request import RequestSchema
from nombot.generics.response import ResponseSchema


@dataclass
class CCXTExchange:
    """Exchange data object"""
    name: str
    currencies: list
    rate_limit: int = None

    _ex = None
    loop = asyncio.get_event_loop()

    avail_currencies = None
    _currencies = None

    avail_markets = None
    markets = None

    avail_symbols = None
    symbols = None

    def __post_init__(self):
        # instantiate exchange object
        self._ex = getattr(ccxt, self.name)()

        if self.rate_limit is not None:
            self._ex.rate_limit = self.rate_limit
        self._ex.enable_rate_limit = True

        self.loop.run_until_complete(self.load())

    async def load(self, reload=False, *args, **kwargs):
        """Load the markets; populating the exchange object with data"""
        if self.markets is not None and not reload:
            return

        self.avail_markets = await self._ex.load_markets(*args, **kwargs)

        self.avail_currencies = getattr(self._ex, "currencies", {})
        if not self.currencies:
            # copy all currencies
            self._currencies = self.avail_currencies
        else:
            # copy relevant currencies
            self._currencies = {
                curr: self.avail_currencies[curr]
                for curr in self.currencies
                if curr in self.avail_currencies
            }

        self.avail_symbols = getattr(self._ex, "symbols", [])
        currencies = self._currencies.keys()
        self.symbols = [
            "/".join(pair)
            for pair in product(currencies, currencies)
            if "/".join(pair) in self.avail_symbols
        ]

        self.markets = {
            sym: self.avail_markets[sym]
            for sym in self.symbols
        }

    async def call_over_syms(self, callname, *args, **kwargs):
        """Cycle through configured exchanges and symbols and make a call"""
        results = {}
        for sym in self.markets.keys():
            try:
                results[sym] = await self.call(callname, sym, *args, **kwargs)
            except (ExchangeNotAvailable, ExchangeError):
                pass
        return results

    async def call(self, callname, *args, **kwargs):
        """Generalized async `call` method, pass callname and parameters"""
        try:
            return await getattr(self._ex, callname)(*args, **kwargs)
        except TypeError:
            raise AttributeError(f"Failed to execute call {callname} on "
                                 f"exchange {self._ex.name}")

    async def close(self):
        """Close all exchange connections"""
        await self._ex.close()

    def shutdown(self):
        """Teardown the aio loop"""
        self.loop.run_until_complete(self.close())
        self.loop.close()


class CCXT:
    """CCXTExchange wrapper"""
    _ex = {}  # type: dict

    def __init__(self, exchanges=None, symbols=None, rate_limit=None):
        if exchanges is None or not exchanges:
            exchanges = ccxt.exchanges

        for exch in exchanges:
            self._ex[exch] = CCXTExchange(exch, symbols, rate_limit)

    def call_on_exchanges(self, callname, *args, **kwargs):
        """Cycle through all configured exchanges to make a call"""
        results = {}
        for ex in self._ex.values():
            try:
                results[ex.name] = ex.loop.run_until_complete(
                    ex.call(callname, *args, **kwargs))
            except (ExchangeNotAvailable, ExchangeError):
                pass
        return results

    def call_over_syms(self, callname, *args, **kwargs):
        """Cycle through configured exchanges and symbols and make a call"""
        results = {}
        for ex in self._ex.values():
            try:
                results[ex.name] = ex.loop.run_until_complete(
                    ex.call_over_syms(callname, *args, **kwargs))
            except (ExchangeNotAvailable, ExchangeError):
                pass
        return results


    def shutdown(self):
        """Shutdown / cleanup"""
        for exch, ex in self._ex.items():
            ex.shutdown()


class CCXTApi(LoggerMixin):  # pylint: disable=R0902
    """
        This class implements ccxt's REST api as documented in the
        documentation available at:
        https://github.com/ccxt/ccxt/wiki/Manual
    """
    name = "ccxt"

    local_overrides = {
        "fetch_order_book": "call_over_syms",
        "default": "call_on_exchanges",  # required
    }

    def __init__(self, context):
        """Launched by Api when we're ready to connect"""
        self.request_schema = RequestSchema
        self.result_schema = ResponseSchema

        self.context = context
        self.conf = context.get("conf")

        # Websocket credentials object
        self.creds = self.context.get("credentials")

        self.create_logger()
        self.log.debug(f"Starting API Facade {self.name}")

        self.ccxt = CCXT(self.conf["exchanges"], self.context["currencies"])

    def call(self, callname, *args, **kwargs):
        """Substitute for REST api as defined in bors.api.requestor.Req"""
        method = self.local_overrides.get(
            callname, self.local_overrides["default"])
        results = getattr(self.ccxt, method)(callname, *args, **kwargs)
        return results

    def shutdown(self):
        """Perform last-minute stuff"""
        self.log.info(f"Shutting down API interface instance for {self.name}")

        # Take care of any currently running tasks in open loops
        (task.cancel() for task in asyncio.Task.all_tasks())

        self.ccxt.shutdown()
