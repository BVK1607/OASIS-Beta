from django.db.models.signals import post_save, post_delete, pre_delete
from django.dispatch import receiver
from .models import Holding, Portfolio, PortfolioNAV, HoldingReturn, BenchmarkReturn, MarketPrice, Asset, GlobalMarketPrice, TradeHistory
from django.utils import timezone
from decimal import Decimal
from portfolio.market_data import get_or_fetch_market_price, fetch_and_store_historical_prices, update_market_prices
from datetime import timedelta, date
import logging
from django.db import transaction
from django.db.utils import OperationalError
import time
import yfinance as yf
from threading import local
from .services import PortfolioNAVService

logger = logging.getLogger(__name__)

# Constants for retry logic
MAX_RETRIES = 3
RETRY_DELAY = 0.5  # seconds

BENCHMARKS = {
    'SP500': '^GSPC',
    'MSCI_WORLD': 'URTH',
    'GLOBAL_AGG': 'AGG'
}

_thread_locals = local()

@receiver(post_delete, sender=Portfolio)
def cleanup_market_prices(sender, instance, **kwargs):
    """Delete market prices when a portfolio is deleted"""
    try:
        # Delete all market prices associated with this portfolio
        MarketPrice.objects.filter(portfolio=instance).delete()
        logger.info(f"Deleted market prices for portfolio {instance.id}")
    except Exception as e:
        logger.error(f"Error deleting market prices for portfolio {instance.id}: {e}")

@receiver(pre_delete, sender=Portfolio)
def predelete_cleanup_portfolio(sender, instance, **kwargs):
    """Delete all related objects before deleting the portfolio to avoid FK constraint errors."""
    try:
        # Delete related objects in order
        HoldingReturn.objects.filter(portfolio=instance).delete()
        PortfolioNAV.objects.filter(portfolio=instance).delete()
        TradeHistory.objects.filter(portfolio=instance).delete()
        MarketPrice.objects.filter(portfolio=instance).delete()
        Holding.objects.filter(portfolio=instance).delete()
        # If you have other related models, add them here
        logger.info(f"Pre-delete cleanup: deleted related objects for portfolio {instance.id}")
    except Exception as e:
        logger.error(f"Error in pre-delete cleanup for portfolio {instance.id}: {e}")

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
                earliest_holding = portfolio.holdings.order_by('purchase_date').first()
                if earliest_holding:
                    start_date = earliest_holding.purchase_date
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
        holdings = portfolio.holdings.filter(purchase_date__lte=date)
        
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
            first_holding = portfolio.holdings.order_by('purchase_date').first()
            if not first_holding:
                logger.warning(f"No holdings found for portfolio {portfolio.id}")
                return
            start_date = first_holding.purchase_date
            
        if start_date > end_date:
            logger.info(f"Portfolio {portfolio.id} already up to date")
            return
            
        logger.info(f"Starting NAV update for portfolio {portfolio.id} from {start_date} to {end_date}")
            
        holdings = portfolio.holdings.filter(purchase_date__lte=end_date)
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
def update_portfolio_data_signal(sender, instance, created, **kwargs):
    """Update portfolio data when a holding is created or updated"""
    try:
        # Skip if we're already in the middle of an update
        if hasattr(instance, '_updating'):
            return
            
        portfolio = instance.portfolio
        date = instance.purchase_date
        
        # Update market prices for all holdings
        holdings = Holding.objects.filter(portfolio=portfolio, purchase_date__lte=date)
        
        for holding in holdings:
            if holding.asset_class == 'Cash':
                continue
                
            # Update market price
            price = get_or_fetch_market_price(holding.ticker, date)
            if price is None:
                logger.error(f"Could not get price for {holding.ticker} on {date}")
                continue
                
            # Update market value
            holding._updating = True
            holding.market_value = price * holding.quantity
            holding.save()
            delattr(holding, '_updating')
            
        # Update portfolio NAV
        total_value = Decimal('0')
        for holding in holdings:
            if holding.market_value is None:
                logger.error(f"No market value for holding {holding.id} on {date}")
                continue
            total_value += holding.market_value
            
        # Create or update NAV record
        nav, created = PortfolioNAV.objects.update_or_create(
            portfolio=portfolio,
            date=date,
            defaults={
                'total_value': total_value,
                'nav': total_value,  # For now, just use total value as NAV
                'last_updated': timezone.now()
            }
        )
        
        # Calculate daily return
        nav.daily_return = nav.calculate_daily_return()
        nav.save()
        
    except Exception as e:
        logger.error(f"Error updating portfolio data: {e}")

@receiver(post_delete, sender=Holding)
def recalculate_portfolio_nav(sender, instance, **kwargs):
    """Recalculate portfolio NAV when a holding is deleted"""
    try:
        portfolio = instance.portfolio
        date = instance.purchase_date
        
        # Update portfolio NAV
        holdings = Holding.objects.filter(portfolio=portfolio, purchase_date__lte=date)
        
        total_value = Decimal('0')
        for holding in holdings:
            if holding.market_value is None:
                logger.error(f"No market value for holding {holding.id} on {date}")
                continue
            total_value += holding.market_value
            
        # Create or update NAV record
        nav, created = PortfolioNAV.objects.update_or_create(
            portfolio=portfolio,
            date=date,
            defaults={
                'total_value': total_value,
                'nav': total_value,  # For now, just use total value as NAV
                'last_updated': timezone.now()
            }
        )
        
        # Calculate daily return
        nav.daily_return = nav.calculate_daily_return()
        nav.save()
        
    except Exception as e:
        logger.error(f"Error recalculating portfolio NAV: {e}")

@receiver(post_save, sender=Portfolio)
def portfolio_post_save(sender, instance, created, **kwargs):
    """Update portfolio data when a portfolio is saved"""
    try:
        # Skip if we're already in the middle of an update
        if hasattr(instance, '_updating'):
            return
            
        # Get the earliest holding date
        first_holding = instance.holdings.order_by('purchase_date').first()
        if not first_holding:
            return
            
        start_date = first_holding.purchase_date
        end_date = date.today()
        
        # Create NAV records from start_date to today
        holdings = instance.holdings.filter(purchase_date__lte=end_date)
        
        for holding in holdings:
            if holding.asset_class == 'Cash':
                continue
                
            # Update market price
            price = get_or_fetch_market_price(holding.ticker, holding.purchase_date)
            if price is None:
                logger.error(f"Could not get price for {holding.ticker} on {holding.purchase_date}")
                continue
                
            # Update market value
            holding._updating = True
            holding.market_value = price * holding.quantity
            holding.save()
            delattr(holding, '_updating')
            
        # Create initial NAV record
        total_value = Decimal('0')
        for holding in holdings:
            if holding.market_value is None:
                logger.error(f"No market value for holding {holding.id}")
                continue
            total_value += holding.market_value
            
        nav = PortfolioNAV.objects.create(
            portfolio=instance,
            date=start_date,
            total_value=total_value,
            nav=total_value,  # For now, just use total value as NAV
            last_updated=timezone.now()
        )
        
    except Exception as e:
        logger.error(f"Error in portfolio_post_save signal handler: {e}")

@receiver(post_save, sender=Portfolio)
def update_holding_market_prices(sender, instance, created, **kwargs):
    """Update market prices for all holdings in the portfolio"""
    # Skip if we're already in the middle of an update
    if hasattr(_thread_locals, 'updating_market_prices'):
        return

    try:
        _thread_locals.updating_market_prices = True
        for holding in instance.holdings.all():
            holding.update_market_price()
    except Exception as e:
        logger.error(f"Error updating market prices for portfolio {instance.id}: {e}")
    finally:
        _thread_locals.updating_market_prices = False

@receiver(post_save, sender=Holding)
def update_holding_market_value(sender, instance, created, **kwargs):
    """Update market value when a holding is saved"""
    try:
        # Skip if we're already in the middle of an update
        if hasattr(instance, '_updating'):
            return
            
        # Skip cash holdings
        if instance.asset_class == 'Cash':
            return
            
        # Get market price
        price = get_or_fetch_market_price(instance.ticker, instance.purchase_date)
        if price is None:
            logger.error(f"Could not get price for {instance.ticker} on {instance.purchase_date}")
            return
            
        # Update market value
        instance._updating = True
        instance.market_value = price * instance.quantity
        instance.save()
        delattr(instance, '_updating')
        
    except Exception as e:
        logger.error(f"Error updating market value for holding {instance.id}: {e}")

@receiver(pre_delete, sender=Holding)
def cleanup_holding_data(sender, instance, **kwargs):
    """
    Clean up any associated data when a holding is deleted
    """
    try:
        # Check if this was the last holding for this asset
        if instance.asset:
            other_holdings = Holding.objects.filter(
                asset=instance.asset
            ).exclude(id=instance.id).exists()
            
            if not other_holdings:
                # No other holdings use this asset, we can clean up old prices
                cutoff_date = timezone.now().date() - timedelta(days=30)
                GlobalMarketPrice.objects.filter(
                    asset=instance.asset,
                    date__lt=cutoff_date
                ).delete()
    except Exception as e:
        logger.error(f"Error cleaning up holding data for {instance.id}: {e}")

@receiver(pre_delete, sender=Asset)
def cleanup_asset_data(sender, instance, **kwargs):
    """
    Clean up market prices when an asset is deleted
    """
    try:
        GlobalMarketPrice.objects.filter(asset=instance).delete()
    except Exception as e:
        logger.error(f"Error cleaning up asset data for {instance.ticker}: {e}")

@receiver(post_save, sender=PortfolioNAV)
def update_nav_return(sender, instance, created, **kwargs):
    """Update daily return when a NAV record is saved"""
    try:
        # Skip if we're already in the middle of an update
        if hasattr(instance, '_updating'):
            return
            
        # Calculate daily return
        instance._updating = True
        instance.daily_return = instance.calculate_daily_return()
        instance.save()
        delattr(instance, '_updating')
        
    except Exception as e:
        logger.error(f"Error updating NAV return: {e}")

@receiver(post_save, sender=Portfolio)
def auto_backfill_nav(sender, instance, created, **kwargs):
    if created:
        PortfolioNAVService.backfill_portfolio_time_series(instance) 