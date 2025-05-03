from django.db.models.signals import post_save, post_delete
from django.dispatch import receiver
from .models import Holding, Portfolio, PortfolioNAV, HoldingReturn, BenchmarkReturn, MarketPrice
from django.utils import timezone
from decimal import Decimal
from portfolio.market_data import get_or_fetch_market_price, fetch_and_store_historical_prices, update_market_prices
from datetime import timedelta, date
import logging
from django.db import transaction
from django.db.utils import OperationalError
import time
import yfinance as yf

logger = logging.getLogger(__name__)

# Constants for retry logic
MAX_RETRIES = 3
RETRY_DELAY = 0.5  # seconds

BENCHMARKS = {
    'SP500': '^GSPC',
    'MSCI_WORLD': 'URTH',
    'GLOBAL_AGG': 'AGG'
}

@receiver(post_delete, sender=Portfolio)
def cleanup_market_prices(sender, instance, **kwargs):
    """Delete market prices when a portfolio is deleted"""
    try:
        # Delete all market prices associated with this portfolio
        MarketPrice.objects.filter(portfolio=instance).delete()
        logger.info(f"Deleted market prices for portfolio {instance.id}")
    except Exception as e:
        logger.error(f"Error deleting market prices for portfolio {instance.id}: {e}")

def ensure_latest_data(portfolio, date=None):
    """Ensure portfolio has up-to-date NAV and returns data."""
    if date is None:
        date = timezone.now().date()
        
    try:
        logger.info(f"Ensuring latest data for portfolio {portfolio.id} up to {date}")
        latest_nav = PortfolioNAV.objects.filter(portfolio=portfolio).order_by('-date').first()

        if not latest_nav or latest_nav.date < date:
            start_date = latest_nav.date + timedelta(days=1) if latest_nav else None
            if start_date is None:
                earliest_holding = portfolio.holdings.order_by('trade_date').first()
                if earliest_holding:
                    start_date = earliest_holding.trade_date
                    logger.info(f"No NAV exists, starting from earliest holding date: {start_date}")
                else:
                    start_date = portfolio.creation_date
                    logger.info(f"No holdings found, starting from portfolio creation date: {start_date}")
            
            logger.info(f"Updating NAV from {start_date} to {date}")
            update_portfolio_data(portfolio, start_date, date)
        else:
            logger.info(f"Portfolio {portfolio.id} NAV is up to date")
            
    except Exception as e:
        logger.error(f"Error ensuring latest data for portfolio {portfolio.id}: {e}")

def calculate_portfolio_nav(portfolio, date, market_values):
    """Calculate portfolio NAV for a given date using market values."""
    try:
        total_value = sum(market_values.values(), Decimal('0.0'))
        logger.info(f"Calculated total value for {date}: {total_value}")
        
        prev_nav = PortfolioNAV.objects.filter(
            portfolio=portfolio,
            date__lt=date
        ).order_by('-date').first()
        
        daily_return = None
        if prev_nav and prev_nav.nav > 0:
            daily_return = ((total_value / prev_nav.nav) - 1) * 100
            logger.info(f"Calculated daily return for {date}: {daily_return}%")
        else:
            logger.info(f"No previous NAV found for {date}, skipping return calculation")
        
        nav, created = PortfolioNAV.objects.update_or_create(
            portfolio=portfolio,
            date=date,
            defaults={
                'nav': total_value,
                'daily_return': daily_return
            }
        )
        
        action = "Created" if created else "Updated"
        logger.info(f"{action} NAV for {date}: {nav.nav}")
        return nav
        
    except Exception as e:
        logger.error(f"Error calculating NAV for portfolio {portfolio.id} on {date}: {e}")
        return None

def calculate_holding_returns(portfolio, date, market_values):
    """Calculate holding returns for a given date."""
    try:
        holdings = portfolio.holdings.filter(trade_date__lte=date)
        
        for holding in holdings:
            if holding.id not in market_values:
                continue
            
            market_value = market_values[holding.id]
            
            prev_return = HoldingReturn.objects.filter(
                holding=holding,
                date__lt=date
            ).order_by('-date').first()
            
            daily_return = None
            if prev_return and prev_return.market_value > 0:
                daily_return = ((market_value / prev_return.market_value) - 1) * 100
            
            for attempt in range(MAX_RETRIES):
                try:
                    with transaction.atomic():
                        HoldingReturn.objects.update_or_create(
                            portfolio=portfolio,
                            holding=holding,
                            date=date,
                            defaults={
                                'market_value': market_value,
                                'daily_return': daily_return
                            }
                        )
                    break
                except OperationalError as e:
                    if attempt == MAX_RETRIES - 1:
                        logger.error(f"Failed to save holding return after {MAX_RETRIES} attempts: {e}")
                        raise
                    time.sleep(RETRY_DELAY)
    except Exception as e:
        logger.error(f"Error calculating returns for portfolio {portfolio.id} on {date}: {e}")

def update_portfolio_data(portfolio, start_date=None, end_date=None):
    """Update portfolio NAV and returns for the specified date range."""
    try:
        if end_date is None:
            end_date = timezone.now().date()
            
        if start_date is None:
            first_holding = portfolio.holdings.order_by('trade_date').first()
            if not first_holding:
                logger.warning(f"No holdings found for portfolio {portfolio.id}")
                return
            start_date = first_holding.trade_date
            
        if start_date > end_date:
            logger.info(f"Portfolio {portfolio.id} already up to date")
            return
            
        logger.info(f"Starting NAV update for portfolio {portfolio.id} from {start_date} to {end_date}")
            
        holdings = portfolio.holdings.filter(trade_date__lte=end_date)
        logger.info(f"Found {holdings.count()} holdings for portfolio {portfolio.id}")
        
        tickers = set(holdings.exclude(asset_class='Cash').values_list('ticker', flat=True))
        logger.info(f"Fetching prices for tickers: {tickers}")
        for ticker in tickers:
            success = fetch_and_store_historical_prices(ticker, start_date, end_date, portfolio=portfolio)
            if not success:
                logger.warning(f"Failed to fetch prices for {ticker}")
        
        current_date = start_date
        while current_date <= end_date:
            if current_date.weekday() >= 5:
                current_date += timedelta(days=1)
                continue
                
            try:
                with transaction.atomic():
                    market_values = update_market_prices(holdings, current_date)
                    logger.info(f"Got market values for {len(market_values)} holdings on {current_date}")
                    
                    if not market_values:
                        logger.warning(f"No market values available for {current_date}")
                        current_date += timedelta(days=1)
                        continue
                    
                    nav = calculate_portfolio_nav(portfolio, current_date, market_values)
                    if nav:
                        logger.info(f"Saved NAV for {current_date}: {nav.nav}")
                    else:
                        logger.warning(f"Failed to calculate NAV for {current_date}")
                    
                    calculate_holding_returns(portfolio, current_date, market_values)
            except Exception as e:
                logger.error(f"Error processing date {current_date} for portfolio {portfolio.id}: {e}")
            
            current_date += timedelta(days=1)
            
    except Exception as e:
        logger.error(f"Error updating portfolio data for {portfolio.id}: {e}")

@receiver(post_save, sender=Holding)
def update_portfolio_nav_and_holding_return(sender, instance, created, **kwargs):
    """Update portfolio NAV and holding returns when a holding is saved"""
    if not instance.portfolio:
        return

    try:
        with transaction.atomic():
            # Get portfolio with lock
            portfolio = Portfolio.objects.select_for_update(nowait=True).get(pk=instance.portfolio.pk)
            
            # Update NAV
            total_market_value = Decimal('0')
            for holding in portfolio.holdings.all():
                if holding.market_value is not None:
                    total_market_value += holding.market_value

            portfolio.nav = total_market_value
            portfolio.save()

            # Update holding returns
            if instance.market_value is not None and instance.book_cost is not None:
                try:
                    holding_return = HoldingReturn.objects.select_for_update(nowait=True).get(
                        holding=instance,
                        date=timezone.now().date()
                    )
                except HoldingReturn.DoesNotExist:
                    holding_return = HoldingReturn(
                        holding=instance,
                        date=timezone.now().date()
                    )

                if instance.book_cost != 0:
                    holding_return.return_value = ((instance.market_value - instance.book_cost) / instance.book_cost) * 100
                else:
                    holding_return.return_value = 0

                holding_return.save()

    except Exception as e:
        logger.error(f"Error updating portfolio NAV and holding returns: {str(e)}")

@receiver(post_delete, sender=Holding)
def update_portfolio_nav_on_delete(sender, instance, **kwargs):
    """
    Update portfolio NAV when a holding is deleted.
    Uses optimistic locking to reduce contention.
    """
    try:
        portfolio = instance.portfolio
        date = instance.trade_date
        
        # Update NAV in smaller batches
        navs = PortfolioNAV.objects.filter(portfolio=portfolio, date=date)
        if navs.exists():
            # Calculate NAV in smaller chunks
            total_nav = Decimal('0')
            holdings = Holding.objects.filter(portfolio=portfolio, trade_date=date)
            for i in range(0, holdings.count(), 5):
                batch = holdings[i:i+5]
                for holding in batch:
                    total_nav += holding.market_value
                    
            navs.update(nav=total_nav)
            
    except Exception as e:
        logger.error(f"Error updating portfolio NAV on delete: {e}")

@receiver(post_save, sender=Portfolio)
def portfolio_post_save(sender, instance, created, **kwargs):
    """Generate initial NAV and returns for new portfolios."""
    if created:
        try:
            update_portfolio_data(
                instance,
                start_date=instance.creation_date,
                end_date=timezone.now().date()
            )
        except Exception as e:
            logger.error(f"Error in portfolio_post_save signal handler: {e}")

@receiver(post_save, sender=Portfolio)
def update_holding_market_prices(sender, instance, created, **kwargs):
    """Update market prices for all holdings in the portfolio"""
    if created:
        return

    try:
        with transaction.atomic():
            # Get holdings with lock
            holdings = instance.holdings.select_for_update(nowait=True).all()
            
            # Update prices in batches
            batch_size = 5
            for i in range(0, len(holdings), batch_size):
                batch = holdings[i:i + batch_size]
                for holding in batch:
                    if holding.asset_class != 'Cash' and holding.ticker:
                        holding.update_market_price()

    except Exception as e:
        logger.error(f"Error updating market prices: {str(e)}")
        
    return holdings 