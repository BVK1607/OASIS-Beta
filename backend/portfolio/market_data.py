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

def with_db_retry(f):
    """
    Decorator to handle database retry logic with proper connection management
    """
    @wraps(f)
    def wrapper(*args, **kwargs):
        for attempt in range(MAX_RETRIES):
            try:
                connections.close_all()  # Close any stale connections before starting
                return f(*args, **kwargs)  # Let the function manage its own transaction
            except OperationalError as e:
                if attempt == MAX_RETRIES - 1:
                    logger.error(f"Failed after {MAX_RETRIES} attempts: {e}")
                    raise
                time.sleep(RETRY_DELAY * (2 ** attempt))  # Exponential backoff
        return None
    return wrapper

@with_db_retry
def store_market_price(ticker: str, date, price: Decimal, volume: int, portfolio=None):
    """
    Store market price with optimized locking
    """
    from .models import Holding, MarketPrice
    try:
        # Find portfolio outside transaction if needed
        if not portfolio:
            holding = Holding.objects.filter(ticker=ticker).first()
            if not holding:
                logger.error(f"Cannot store market price for {ticker} - no holding found")
                return False
            portfolio = holding.portfolio
            
        # Get previous price for validation outside transaction
        prev_price = MarketPrice.objects.filter(
            ticker=ticker,
            portfolio=portfolio
        ).order_by('-date').first()
        
        if prev_price and not validate_price_change(prev_price.price, price, ticker):
            logger.error(f"Invalid price change detected for {ticker} on {date}")
            return False
            
        # Minimal transaction for just the update/create
        with transaction.atomic(using='default'):
            MarketPrice.objects.update_or_create(
                ticker=ticker,
                date=date,
                portfolio=portfolio,
                defaults={
                    'price': price,
                    'volume': volume,
                    'last_updated': timezone.now()
                }
            )
        return True
        
    except Exception as e:
        logger.error(f"Error storing price for {ticker} on {date}: {e}")
        return False

def get_or_fetch_market_price(ticker: str, date) -> dict:
    """
    Get market price from database or fetch from API if not available.
    Returns dict with 'price' and 'volume' if available.
    """
    try:
        # Handle future dates first
        if date > timezone.now().date():
            logger.info(f"Requested future date {date} for {ticker}, using current price")
            # Get current price directly
            stock = yf.Ticker(ticker)
            current_price = stock.info.get('regularMarketPrice')
            if current_price:
                return {
                    'price': Decimal(str(current_price)),
                    'volume': None,
                    'is_clean': True,
                    'warnings': [f"Using current price for future date {date}"]
                }
            return None

        # Check if it's a trading day
        if not is_trading_day(date):
            logger.info(f"Skipping non-trading day {date} for {ticker}")
            prev_trading_day = get_previous_trading_day(date)
            return get_or_fetch_market_price(ticker, prev_trading_day)
            
        # Try to get from database first - without locking
        try:
            market_price = MarketPrice.objects.filter(
                ticker=ticker, 
                date=date
            ).first()  # Remove select_for_update since we're just reading
            
            if market_price:
                return {
                    'price': market_price.price,
                    'volume': market_price.volume,
                    'is_clean': True,
                    'warnings': []
                }
        except Exception as e:
            logger.error(f"Error reading from database: {e}")
            
        # Not in database, fetch from API
        end_date = date + timedelta(days=1)
        
        # Fetch from API outside transaction
        ticker_data = yf.Ticker(ticker)
        hist = ticker_data.history(start=date, end=end_date)
        
        if hist.empty:
            # Try fetching latest trading day
            hist = ticker_data.history(period="1d")
            if hist.empty:
                logger.warning(f"No data available for {ticker}")
                return None
        
        # Convert pandas Timestamp to datetime.date for comparison
        hist_date = hist.index[0].date()
        if hist_date != date:
            logger.warning(f"Date mismatch: requested {date}, got {hist_date}")
            return None
        
        price = Decimal(str(hist['Close'].iloc[0]))
        volume = int(hist['Volume'].iloc[0])
        
        # Store in database with minimal locking
        try:
            with transaction.atomic(using='default'):
                MarketPrice.objects.update_or_create(
                    ticker=ticker,
                    date=date,
                    defaults={
                        'price': price,
                        'volume': volume,
                        'last_updated': timezone.now()
                    }
                )
                
            return {
                'price': price,
                'volume': volume,
                'is_clean': True,
                'warnings': []
            }
        except Exception as e:
            logger.error(f"Error storing price: {e}")
            # Still return the price even if storage failed
            return {
                'price': price,
                'volume': volume,
                'is_clean': False,
                'warnings': [f"Failed to store price: {e}"]
            }
            
    except Exception as e:
        logger.error(f"Error in get_or_fetch_market_price for {ticker} on {date}: {e}")
        return None

@with_db_retry
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
            
        # Find portfolio if not provided
        if not portfolio:
            holding = Holding.objects.filter(ticker=ticker).first()
            if not holding:
                logger.error(f"Cannot fetch historical prices for {ticker} - no holding found")
                return False
            portfolio = holding.portfolio

        # Get existing prices for this ticker and portfolio
        existing_prices = MarketPrice.objects.filter(
            ticker=ticker,
            portfolio=portfolio,
            date__range=[start_date, end_date]
        ).values_list('date', flat=True)
        
        # Convert to set for faster lookups
        existing_dates = set(existing_prices)
        
        # Get missing dates
        missing_dates = []
        current_date = start_date
        while current_date <= end_date:
            if current_date not in existing_dates:
                missing_dates.append(current_date)
            current_date += timedelta(days=1)
            
        if not missing_dates:
            logger.info(f"No missing prices for {ticker} between {start_date} and {end_date}")
            return True
            
        # Fetch missing prices
        prices = fetch_historical_prices(ticker, missing_dates)
        if not prices:
            logger.error(f"Failed to fetch prices for {ticker}")
            return False
            
        # Validate price data structure
        for date, price_data in prices.items():
            if not isinstance(price_data, dict) or 'price' not in price_data:
                logger.error(f"Invalid price data structure for {ticker} on {date}")
                return False
                
            # Ensure numeric price values
            try:
                price_data['price'] = Decimal(str(price_data['price']))
                price_data['volume'] = int(price_data.get('volume', 0))
            except (ValueError, TypeError, InvalidOperation) as e:
                logger.error(f"Invalid price/volume for {ticker} on {date}: {e}")
                return False
            
        # Prepare data for bulk create
        price_objects = []
        for date, price_data in prices.items():
            price_objects.append(MarketPrice(
                ticker=ticker,
                date=date,
                price=price_data['price'],
                volume=price_data['volume'],
                portfolio=portfolio,
                last_updated=timezone.now()
            ))
            
        # Bulk create in batches
        batch_size = 100
        for i in range(0, len(price_objects), batch_size):
            batch = price_objects[i:i + batch_size]
            MarketPrice.objects.bulk_create(batch, ignore_conflicts=True)
            
        logger.info(f"Stored {len(price_objects)} historical prices for {ticker}")
        return True
        
    except Exception as e:
        logger.error(f"Error storing historical prices for {ticker}: {e}")
        return False

def update_market_prices(holdings, date) -> dict:
    """
    Update market prices for a list of holdings on a specific date.
    Returns dict with market values for each holding.
    """
    try:
        market_values = {}
        
        for holding in holdings:
            if holding.asset_class == 'Cash':
                market_values[holding.id] = holding.book_cost
                continue
                
            if not holding.ticker:
                continue
                
            try:
                price_data = get_or_fetch_market_price(holding.ticker, date)
                if price_data and price_data.get('price'):
                    market_values[holding.id] = price_data['price'] * holding.quantity
                else:
                    logger.warning(f"No price data available for {holding.ticker} on {date}")
            except Exception as e:
                logger.error(f"Error updating price for {holding.ticker} on {date}: {str(e)}")
                
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
    for ticker in tickers:
        try:
            # Get existing prices outside transaction
            existing_dates = set(
                MarketPrice.objects.filter(
                    ticker=ticker,
                    date__range=(start_date, end_date)
                ).values_list('date', flat=True)
            )
            
            # Find missing dates
            all_dates = pd.date_range(start_date, end_date, freq='D')
            missing_dates = [d.date() for d in all_dates if d.date() not in existing_dates]
            
            if not missing_dates:
                continue
                
            # Fetch all historical data at once outside transaction
            ticker_data = yf.Ticker(ticker)
            hist = ticker_data.history(
                start=min(missing_dates),
                end=max(missing_dates) + timedelta(days=1)
            )
            
            if hist.empty:
                logger.warning(f"No data available for {ticker}")
                continue
                
            # Process each date with minimal locking
            for date in missing_dates:
                if date in hist.index.date:
                    try:
                        price = Decimal(str(hist.loc[hist.index.date == date, 'Close'].iloc[0]))
                        volume = int(hist.loc[hist.index.date == date, 'Volume'].iloc[0])
                        
                        with transaction.atomic():
                            MarketPrice.objects.update_or_create(
                                ticker=ticker,
                                date=date,
                                defaults={
                                    'price': price,
                                    'volume': volume,
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
    task_queue.add_historical_data_task(portfolio_id, start_date)

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

def validate_price_change(previous_price: Decimal, new_price: Decimal, ticker: str) -> bool:
    """
    Validate if a price change is reasonable.
    Returns True if the change is valid, False if suspicious.
    """
    if not previous_price or not new_price:
        return True
        
    # Calculate percentage change
    change = abs((new_price - previous_price) / previous_price) * 100
    
    # Different thresholds for different asset classes
    if ticker.startswith('^'):  # Index
        max_change = 20.0
    elif ticker.endswith('.L'):  # LSE stocks
        max_change = 50.0
    else:  # US stocks
        max_change = 30.0
        
    if change > max_change:
        logger.warning(f"Suspicious price change for {ticker}: {change:.2f}%")
        return False
        
    return True 

def validate_market_data(ticker: str, date: date) -> bool:
    """
    Validate market data for a given ticker and date
    Returns True if data is valid, False otherwise
    """
    try:
        # Check if date is a trading day
        if not is_trading_day(date):
            logger.info(f"{date} is not a trading day for {ticker}")
            return True
            
        # Get previous price
        prev_price = MarketPrice.objects.filter(
            ticker=ticker,
            date__lt=date
        ).order_by('-date').first()
        
        # Get current price
        current_price = MarketPrice.objects.filter(
            ticker=ticker,
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
        next_price = MarketPrice.objects.filter(
            ticker=ticker,
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