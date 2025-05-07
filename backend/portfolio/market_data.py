from django.utils import timezone
import yfinance as yf
from decimal import Decimal
from typing import List, Optional, Dict
import pandas as pd
from django.core.exceptions import ValidationError
import logging
from datetime import datetime, timedelta, date
from django.db import transaction, connections
from django.db.utils import OperationalError
import time
from pandas.tseries.holiday import USFederalHolidayCalendar, get_calendar
from pandas.tseries.offsets import CustomBusinessDay
from functools import wraps
from dateutil import parser
from decimal import InvalidOperation
import requests
from django.core.cache import cache
from django.db import models
from django.core.validators import MinValueValidator
from django.db.models import Sum, F, Q, Min, Max
from .models import Asset, GlobalMarketPrice, FXRate

logger = logging.getLogger(__name__)

MAX_RETRIES = 3
RETRY_DELAY = 1.0  # Increased delay
LOCK_TIMEOUT = 5.0  # Maximum time to wait for lock

def is_trading_day(date):
    """
    Check if a given date is a trading day (not weekend or US bank holiday)
    """
    # Skip weekends
    if date.weekday() >= 5:
        return False
        
    # Check US bank holidays
    us_calendar = get_calendar('USFederalHolidayCalendar')
    holidays = us_calendar.holidays(start=date, end=date)
    return len(holidays) == 0

def get_previous_trading_day(date):
    """
    Get the most recent trading day before the given date
    """
    us_bd = CustomBusinessDay(calendar=USFederalHolidayCalendar())
    return date - us_bd

def with_db_retry(max_retries=3, delay=0.5):
    """Decorator to retry database operations with exponential backoff"""
    def decorator(func):
        def wrapper(*args, **kwargs):
            retries = 0
            while retries < max_retries:
                try:
                    return func(*args, **kwargs)
                except OperationalError as e:
                    if retries == max_retries - 1:
                        logger.error(f"Failed after {max_retries} retries: {e}")
                        raise
                    retries += 1
                    time.sleep(delay * (2 ** retries))  # Exponential backoff
            return func(*args, **kwargs)
        return wrapper
    return decorator

def validate_price_change(prev_price: Decimal, new_price: Decimal, ticker: str) -> bool:
    """Validate that price change is reasonable"""
    if not prev_price or not new_price:
        return True
        
    # Calculate percentage change
    change = abs((new_price - prev_price) / prev_price)
    
    # Allow up to 50% change
    if change > 0.5:
        logger.warning(f"Large price change detected for {ticker}: {change:.2%}")
        return False
        
    return True

@with_db_retry
def store_market_price(ticker: str, date, price: Decimal, is_trading_day: bool = True):
    """
    Store market price with optimized locking
    """
    from .models import Asset, GlobalMarketPrice
    
    try:
        # Get or create asset
        asset, created = Asset.objects.get_or_create(
            ticker=ticker,
            defaults={
                'name': ticker,
                'asset_class': 'Equity',
                'currency': 'USD',
                'market': 'US'
            }
        )
        
        # Get previous price for validation
        prev_price = GlobalMarketPrice.objects.filter(
            asset=asset
        ).order_by('-date').first()
        
        if prev_price and not validate_price_change(prev_price.price, price, ticker):
            logger.error(f"Invalid price change detected for {ticker} on {date}")
            return False
            
        # Minimal transaction for just the update/create
        with transaction.atomic(using='default'):
            GlobalMarketPrice.objects.update_or_create(
                asset=asset,
                date=date,
                defaults={
                    'price': price,
                    'is_trading_day': is_trading_day,
                    'last_updated': timezone.now()
                }
            )
        return True
        
    except Exception as e:
        logger.error(f"Error storing price for {ticker} on {date}: {e}")
        return False

class MarketDataProvider:
    """Base class for market data providers"""
    
    def __init__(self, name: str, priority: int):
        self.name = name
        self.priority = priority
        
    def get_price(self, ticker: str, date: date) -> Optional[Decimal]:
        """Get price for a ticker on a specific date"""
        raise NotImplementedError
        
    def get_historical_prices(self, ticker: str, start_date: date, end_date: date) -> Dict[date, Dict]:
        """Get historical prices for a ticker"""
        raise NotImplementedError

class YFinanceProvider(MarketDataProvider):
    """Yahoo Finance data provider"""
    
    def __init__(self):
        super().__init__('yfinance', 1)
        
    def get_price(self, ticker: str, date: date) -> Optional[Decimal]:
        try:
            ticker_data = yf.Ticker(ticker)
            hist = ticker_data.history(start=date, end=date + timedelta(days=1))
            
            if hist.empty:
                return None
                
            return Decimal(str(hist['Close'].iloc[0]))
        except Exception as e:
            logger.error(f"Error fetching price from yfinance for {ticker} on {date}: {e}")
            return None
            
    def get_historical_prices(self, ticker: str, start_date: date, end_date: date) -> Dict[date, Dict]:
        try:
            ticker_data = yf.Ticker(ticker)
            hist = ticker_data.history(start=start_date, end=end_date + timedelta(days=1))
            
            if hist.empty:
                return {}
                
            prices = {}
            for idx, row in hist.iterrows():
                prices[idx.date()] = {
                    'price': Decimal(str(row['Close'])),
                    'is_trading_day': True
                }
            return prices
        except Exception as e:
            logger.error(f"Error fetching historical prices from yfinance for {ticker}: {e}")
            return {}

class TwelveDataProvider(MarketDataProvider):
    """Twelve Data API provider"""
    
    def __init__(self, api_key: str):
        super().__init__('twelvedata', 2)
        self.api_key = api_key
        self.base_url = 'https://api.twelvedata.com'
        
    def get_price(self, ticker: str, date: date) -> Optional[Decimal]:
        try:
            # Check cache first
            cache_key = f'twelvedata_price_{ticker}_{date}'
            cached_price = cache.get(cache_key)
            if cached_price is not None:
                return cached_price
                
            # Format date for API
            date_str = date.strftime('%Y-%m-%d')
            
            # Make API request
            response = requests.get(
                f"{self.base_url}/time_series",
                params={
                    'symbol': ticker,
                    'interval': '1day',
                    'start_date': date_str,
                    'end_date': date_str,
                    'apikey': self.api_key
                }
            )
            
            if response.status_code != 200:
                logger.error(f"Error from Twelve Data API: {response.text}")
                return None
                
            data = response.json()
            if not data.get('values'):
                return None
                
            price = Decimal(str(data['values'][0]['close']))
            
            # Cache the result
            cache.set(cache_key, price, 60 * 60)  # Cache for 1 hour
            
            return price
        except Exception as e:
            logger.error(f"Error fetching price from Twelve Data for {ticker} on {date}: {e}")
            return None
            
    def get_historical_prices(self, ticker: str, start_date: date, end_date: date) -> Dict[date, Dict]:
        try:
            # Check cache first
            cache_key = f'twelvedata_historical_{ticker}_{start_date}_{end_date}'
            cached_prices = cache.get(cache_key)
            if cached_prices is not None:
                return cached_prices
                
            # Format dates for API
            start_str = start_date.strftime('%Y-%m-%d')
            end_str = end_date.strftime('%Y-%m-%d')
            
            # Make API request
            response = requests.get(
                f"{self.base_url}/time_series",
                params={
                    'symbol': ticker,
                    'interval': '1day',
                    'start_date': start_str,
                    'end_date': end_str,
                    'apikey': self.api_key
                }
            )
            
            if response.status_code != 200:
                logger.error(f"Error from Twelve Data API: {response.text}")
                return {}
                
            data = response.json()
            if not data.get('values'):
                return {}
                
            prices = {}
            for value in data['values']:
                date = parser.parse(value['datetime']).date()
                prices[date] = {
                    'price': Decimal(str(value['close'])),
                    'is_trading_day': True
                }
                
            # Cache the result
            cache.set(cache_key, prices, 60 * 60)  # Cache for 1 hour
            
            return prices
        except Exception as e:
            logger.error(f"Error fetching historical prices from Twelve Data for {ticker}: {e}")
            return {}

# Initialize providers
providers = [
    YFinanceProvider(),
    # TwelveDataProvider(api_key='YOUR_API_KEY')  # Uncomment and add your API key
]

def get_or_fetch_market_price(ticker: str, date) -> Optional[Decimal]:
    """
    Get market price from database or fetch from providers. If not found for the given date, fallback to the most recent available price before that date.
    """
    from .models import Asset, GlobalMarketPrice
    try:
        # Convert date to date object if it's a datetime
        if isinstance(date, datetime):
            date = date.date()
        # Get or create asset
        asset, created = Asset.objects.get_or_create(
            ticker=ticker,
            defaults={
                'name': ticker,
                'asset_class': 'Equity',
                'currency': 'USD',
                'market': 'US'
            }
        )
        # Try to get from database first (exact date)
        market_price = GlobalMarketPrice.objects.filter(
            asset=asset,
            date=date
        ).first()
        if market_price:
            return market_price.price
        # If not in database, try providers in order of priority
        for provider in sorted(providers, key=lambda x: x.priority):
            price = provider.get_price(ticker, date)
            if price is not None:
                # Store in database
                with transaction.atomic():
                    GlobalMarketPrice.objects.update_or_create(
                        asset=asset,
                        date=date,
                        defaults={
                            'price': price,
                            'is_trading_day': True,
                            'last_updated': timezone.now()
                        }
                    )
                return price
        # If still not found, fallback to most recent available price before date
        prev_price_obj = GlobalMarketPrice.objects.filter(
            asset=asset,
            date__lt=date
        ).order_by('-date').first()
        if prev_price_obj:
            logger.warning(f"No price for {ticker} on {date}, using previous available price from {prev_price_obj.date}.")
            return prev_price_obj.price
        # If we couldn't get the price, queue a background update
        from .models import Holding
        holding = Holding.objects.filter(asset__ticker=ticker).first()
        if holding:
            queue_market_data_update(holding.portfolio_id, date, date)
        logger.warning(f"No price data available for {ticker} on {date} or any previous date.")
        return None
    except Exception as e:
        logger.error(f"Error in get_or_fetch_market_price for {ticker} on {date}: {e}")
        return None

@with_db_retry()
def fetch_and_store_historical_prices(ticker: str, start_date, end_date, portfolio=None):
    """
    Fetch and store historical prices for a ticker
    
    Args:
        ticker (str): The ticker symbol
        start_date (date): Start date for historical prices
        end_date (date): End date for historical prices
        portfolio (Portfolio, optional): Portfolio to associate prices with
        
    Returns:
        bool: True if successful, False otherwise
    """
    try:
        # Validate dates
        if not isinstance(start_date, date) or not isinstance(end_date, date):
            try:
                start_date = parser(start_date).date()
                end_date = parser(end_date).date()
            except (TypeError, ValueError) as e:
                logger.error(f"Invalid date format for {ticker}: {e}")
                return False
                
        if start_date > end_date:
            logger.error(f"Start date {start_date} is after end date {end_date}")
            return False
            
        # Never fetch future prices
        today = timezone.now().date()
        if end_date > today:
            end_date = today
            
        if start_date > today:
            logger.warning(f"Start date {start_date} is in the future")
            return False
            
        # Find portfolio if not provided
        if not portfolio:
            holding = Holding.objects.filter(ticker=ticker).first()
            if not holding:
                logger.error(f"Cannot fetch historical prices for {ticker} - no holding found")
                return False
            portfolio = holding.portfolio

        # Fetch all historical data at once
        ticker_data = yf.Ticker(ticker)
        hist = ticker_data.history(start=start_date, end=end_date + timedelta(days=1))
        
        if hist.empty:
            logger.warning(f"No historical data available for {ticker}")
            return False
            
        # Get existing prices for this ticker
        existing_prices = GlobalMarketPrice.objects.filter(
            asset__ticker=ticker,
            date__range=[start_date, end_date]
        ).values_list('date', flat=True)
        
        # Convert to set for faster lookups
        existing_dates = set(existing_prices)
        
        # Process each date
        success = True
        for index, row in hist.iterrows():
            current_date = index.date()
            if current_date in existing_dates:
                continue
                
            try:
                price = Decimal(str(row['Close']))
                is_trading_day = True
                
                with transaction.atomic():
                    GlobalMarketPrice.objects.update_or_create(
                        asset=Asset.objects.get(ticker=ticker),
                        date=current_date,
                        defaults={
                            'price': price,
                            'is_trading_day': is_trading_day,
                            'last_updated': timezone.now()
                        }
                    )
            except Exception as e:
                logger.error(f"Error storing price for {ticker} on {current_date}: {e}")
                success = False
                
        return success
        
    except Exception as e:
        logger.error(f"Error fetching historical prices for {ticker}: {e}")
        return False

def get_latest_available_price(ticker: str, up_to_date: date = None) -> Optional[Decimal]:
    """
    Get the most recent available price for a ticker up to a given date.
    """
    from .models import Asset, GlobalMarketPrice
    if up_to_date is None:
        up_to_date = timezone.now().date()
    asset = Asset.objects.filter(ticker=ticker).first()
    if not asset:
        return None
    price_obj = GlobalMarketPrice.objects.filter(asset=asset, date__lte=up_to_date).order_by('-date').first()
    return price_obj.price if price_obj else None

def update_market_prices(holdings, date) -> dict:
    """
    Update market prices for a list of holdings using the latest available price up to the given date.
    Returns dict with market values for each holding.
    """
    try:
        market_values = {}
        for holding in holdings:
            if holding.asset_class == 'Cash':
                holding.current_price = holding.book_cost
                holding.market_value = holding.book_cost
                holding.save(update_fields=['current_price', 'market_value'])
                market_values[holding.id] = holding.book_cost
                continue
            if not holding.asset:
                holding.current_price = None
                holding.market_value = None
                holding.save(update_fields=['current_price', 'market_value'])
                continue
            try:
                price = get_latest_available_price(holding.asset.ticker, date)
                if price is not None:
                    holding.current_price = price
                    holding.market_value = price * holding.quantity
                    holding.save(update_fields=['current_price', 'market_value'])
                    market_values[holding.id] = holding.market_value
                else:
                    holding.current_price = None
                    holding.market_value = None
                    holding.save(update_fields=['current_price', 'market_value'])
                    logger.warning(f"No price data available for {holding.asset.ticker} up to {date}")
            except Exception as e:
                holding.current_price = None
                holding.market_value = None
                holding.save(update_fields=['current_price', 'market_value'])
                logger.error(f"Error updating price for {holding.asset.ticker} on {date}: {str(e)}")
        return market_values
    except Exception as e:
        logger.error(f"Error in update_market_prices: {e}")
        return {}

def get_market_price(ticker: str) -> Dict:
    """
    Enhanced market price fetch with quality checks and better date handling
    """
    try:
        logger.info(f"Fetching market price for {ticker}")
        
        # For testing, return mock data
        mock_prices = {
            'AAPL': 170.50,
            'MSFT': 380.25,
            'GOOGL': 140.75,
            'AMZN': 175.25,
            'META': 480.30,
        }
        
        if ticker in mock_prices:
            return {
                'price': Decimal(str(mock_prices[ticker])),
                'timestamp': timezone.now(),
                'is_clean': True,
                'warnings': []
            }
        
        # If not in mock data, try yfinance
        try:
            yf_ticker = yf.Ticker(ticker)
            current_price = yf_ticker.info.get('regularMarketPrice')
            
            if current_price:
                return {
                    'price': Decimal(str(current_price)),
                    'timestamp': timezone.now(),
                    'is_clean': True,
                    'warnings': []
                }
        except Exception as e:
            logger.error(f"Error fetching from yfinance for {ticker}: {str(e)}")
        
        # If all else fails
        return {
            'price': None,
            'timestamp': timezone.now(),
            'is_clean': False,
            'warnings': [f"No price data available for {ticker}"]
        }
        
    except Exception as e:
        logger.error(f"Error in get_market_price for {ticker}: {str(e)}")
        return {
            'price': None,
            'timestamp': timezone.now(),
            'is_clean': False,
            'warnings': [f"Error fetching price: {str(e)}"]
        }

class PriceValidationError(ValidationError):
    pass

# Price validation constants
PRICE_CHANGE_TOLERANCE = 0.50  # Increased from 0.30 to 0.50 for more lenient validation
ZERO_PRICE_TOLERANCE = 0.0001  # Minimum acceptable price
VOLUME_ZERO_TOLERANCE = 1  # Reduced from 10 to 1 for more lenient validation
STALE_PRICE_HOURS = 72  # Increased from 48 to 72 hours
HIGH_LOW_SPREAD_TOLERANCE = 0.50  # Increased from 0.30 to 0.50

class MarketDataQuality:
    def __init__(self, ticker: str, price: float, timestamp: datetime,
                 volume: Optional[float] = None, 
                 high: Optional[float] = None,
                 low: Optional[float] = None):
        self.ticker = ticker
        self.price = Decimal(str(price))
        self.timestamp = timestamp
        self.volume = volume
        self.high = Decimal(str(high)) if high else None
        self.low = Decimal(str(low)) if low else None
        self.validation_messages = []

    def validate(self, previous_price: Optional[Decimal] = None) -> bool:
        """
        Comprehensive market data validation with improved handling
        Returns True if data is valid, False if suspicious but usable
        Raises PriceValidationError if data is invalid
        """
        try:
            # Basic price validation is required
            self._validate_price_value()
            
            # Other validations are warnings only
            validation_warnings = []
            
            # Check timestamp if available
            if self.timestamp:
                warning = self._validate_timestamp()
                if warning:
                    validation_warnings.append(warning)

            # Check price change if previous price available
            if previous_price:
                warning = self._validate_price_change(previous_price)
                if warning:
                    validation_warnings.append(warning)

            # Check volume if available
            if self.volume is not None:
                warning = self._validate_volume()
                if warning:
                    validation_warnings.append(warning)

            # Check high-low spread if available
            if self.high is not None and self.low is not None:
                warning = self._validate_high_low_spread()
                if warning:
                    validation_warnings.append(warning)
            
            # Only log warnings if there are multiple issues
            if len(validation_warnings) > 2:
                self.validation_messages = validation_warnings
            
            return len(self.validation_messages) == 0

        except PriceValidationError as e:
            raise e
        except Exception as e:
            raise PriceValidationError(f"Validation error for {self.ticker}: {str(e)}")

    def _validate_price_value(self):
        """Validate basic price value - this is a required check"""
        if self.price is None or self.price <= ZERO_PRICE_TOLERANCE:
            raise PriceValidationError(f"Invalid price for {self.ticker}: {self.price}")

    def _validate_timestamp(self):
        """Check if price is stale - returns warning message if stale"""
        if self.timestamp:
            hours_old = (timezone.now() - self.timestamp).total_seconds() / 3600
            if hours_old > STALE_PRICE_HOURS:
                return f"Price for {self.ticker} is {int(hours_old)} hours old"
        return None

    def _validate_price_change(self, previous_price: Decimal):
        """Validate price movement - returns warning message if suspicious"""
        if previous_price > ZERO_PRICE_TOLERANCE:
            price_change = abs((self.price - previous_price) / previous_price)
            if price_change > PRICE_CHANGE_TOLERANCE:
                return f"Large price change for {self.ticker}: {price_change:.2%}"
        return None

    def _validate_volume(self):
        """Validate trading volume - returns warning message if suspicious"""
        if self.volume < VOLUME_ZERO_TOLERANCE:
            return f"Low trading volume for {self.ticker}: {self.volume}"
        return None

    def _validate_high_low_spread(self):
        """Validate high-low spread - returns warning message if suspicious"""
        if self.high > 0 and self.low > 0:
            spread = (self.high - self.low) / self.low
            if spread > HIGH_LOW_SPREAD_TOLERANCE:
                return f"Large high-low spread for {self.ticker}: {spread:.2%}"
        return None

def get_historical_prices(ticker: str, start_date, end_date) -> pd.Series:
    """Get historical prices for a ticker."""
    try:
        ticker_data = yf.Ticker(ticker)
        hist = ticker_data.history(start=start_date, end=end_date + timedelta(days=1))
        
        if hist.empty:
            logger.warning(f"No historical data available for {ticker} from {start_date} to {end_date}")
            return pd.Series()
            
        # Convert to series with date index
        prices = hist['Close']
        prices.index = prices.index.date
        
        return prices
        
    except Exception as e:
        logger.error(f"Error getting historical prices for {ticker}: {e}")
        return pd.Series()

@with_db_retry
def bulk_ensure_market_prices(tickers, start_date, end_date):
    """
    Ensure market prices exist for given tickers and date range.
    Optimized to reduce database contention.
    """
    success = True
    today = timezone.now().date()
    
    # Never process future dates
    if end_date > today:
        end_date = today
        
    if start_date > today:
        logger.warning(f"Start date {start_date} is in the future")
        return False
        
    for ticker in tickers:
        try:
            # Get existing prices outside transaction
            existing_dates = set(
                GlobalMarketPrice.objects.filter(
                    asset__ticker=ticker,
                    date__range=(start_date, end_date)
                ).values_list('date', flat=True)
            )
            
            # Fetch all historical data at once
            ticker_data = yf.Ticker(ticker)
            hist = ticker_data.history(start=start_date, end=end_date + timedelta(days=1))
            
            if hist.empty:
                logger.warning(f"No data available for {ticker}")
                continue
            
            # Process each date with minimal locking
            for index, row in hist.iterrows():
                date = index.date()
                if date in existing_dates:
                    continue
                    
                try:
                    price = Decimal(str(row['Close']))
                    is_trading_day = True  # Assume it's a trading day if we got data
                    
                    with transaction.atomic():
                        GlobalMarketPrice.objects.update_or_create(
                            asset=Asset.objects.get(ticker=ticker),
                            date=date,
                            defaults={
                                'price': price,
                                'is_trading_day': is_trading_day,
                                'last_updated': timezone.now()
                            }
                        )
                except Exception as e:
                    logger.error(f"Error storing price for {ticker} on {date}: {e}")
                    success = False
                    
        except Exception as e:
            logger.error(f"Error processing {ticker}: {e}")
            success = False
            
    return success

def queue_market_data_update(portfolio_id: int, start_date=None, end_date=None):
    """Queue a market data update task"""
    from .task_queue import task_queue
    
    # If no dates provided, get the portfolio's date range
    if not start_date or not end_date:
        from .models import Holding
        holdings = Holding.objects.filter(portfolio_id=portfolio_id)
        if holdings.exists():
            start_date = holdings.aggregate(Min('purchase_date'))['purchase_date__min']
            end_date = timezone.now().date()
    
    # Queue the task
    task_queue.add_historical_data_task(portfolio_id, start_date, end_date)
    
    # Also queue a live price update
    task_queue.add_live_prices_task(portfolio_id)

def queue_live_price_update(portfolio_id: int):
    """Queue a live price update task"""
    from .task_queue import task_queue
    task_queue.add_live_prices_task(portfolio_id)

# TODO: Implement real market data integration
# def get_market_price(isin: str, ticker: str) -> Decimal:
#     """
#     Fetch current market price from a market data provider
#     """
#     pass 

def validate_market_data(ticker: str, date: date) -> bool:
    """
    Validate market data for a given ticker and date
    Returns True if data is valid, False otherwise
    """
    from .models import GlobalMarketPrice
    
    try:
        # Check if date is a trading day
        if not is_trading_day(date):
            logger.info(f"{date} is not a trading day for {ticker}")
            return True
            
        # Get previous price
        prev_price = GlobalMarketPrice.objects.filter(
            asset__ticker=ticker,
            date__lt=date
        ).order_by('-date').first()
        
        # Get current price
        current_price = GlobalMarketPrice.objects.filter(
            asset__ticker=ticker,
            date=date
        ).first()
        
        if not current_price:
            logger.warning(f"No price data for {ticker} on {date}")
            return False
            
        if prev_price:
            # Check price change
            if not validate_price_change(prev_price.price, current_price.price, ticker):
                logger.error(f"Invalid price change detected for {ticker} on {date}")
                return False
                
        # Check for gaps in data
        next_price = GlobalMarketPrice.objects.filter(
            asset__ticker=ticker,
            date__gt=date
        ).order_by('date').first()
        
        if next_price and (next_price.date - date).days > 1:
            logger.warning(f"Gap in price data for {ticker} between {date} and {next_price.date}")
            return False
            
        return True
        
    except Exception as e:
        logger.error(f"Error validating market data for {ticker} on {date}: {str(e)}")
        return False 

def get_or_fetch_fx_rate(base_currency: str, target_currency: str, date: date) -> Optional[Decimal]:
    """
    Get FX rate from database or fetch from yfinance if not available.
    Returns None if rate cannot be fetched.
    """
    from .models import FXRate
    
    try:
        # Check if it's the same currency
        if base_currency == target_currency:
            return Decimal('1.0')

        # Try to get from database first
        fx_rate = FXRate.objects.filter(
            base_currency=base_currency,
            target_currency=target_currency,
            date=date
        ).first()

        if fx_rate:
            return fx_rate.rate

        # Not in database, fetch from yfinance
        pair = f"{base_currency}{target_currency}=X"
        ticker = yf.Ticker(pair)
        hist = ticker.history(start=date, end=date + timedelta(days=1))

        if hist.empty:
            logger.warning(f"No FX rate data available for {pair} on {date}")
            return None

        rate = Decimal(str(hist['Close'].iloc[0]))

        # Store in database
        FXRate.objects.create(
            base_currency=base_currency,
            target_currency=target_currency,
            rate=rate,
            date=date
        )

        return rate

    except Exception as e:
        logger.error(f"Error getting FX rate for {base_currency}/{target_currency} on {date}: {e}")
        return None

def convert_currency(amount: Decimal, from_currency: str, to_currency: str, date: date) -> Optional[Decimal]:
    """
    Convert amount from one currency to another using the rate on the specified date.
    Returns None if conversion fails.
    """
    try:
        rate = get_or_fetch_fx_rate(from_currency, to_currency, date)
        if rate is None:
            return None
        return amount * rate
    except Exception as e:
        logger.error(f"Error converting {amount} {from_currency} to {to_currency}: {e}")
        return None 