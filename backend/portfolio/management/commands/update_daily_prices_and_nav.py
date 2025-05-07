from django.core.management.base import BaseCommand
from django.utils import timezone
from django.db import transaction
from django.db.models import Min, Max, Q
from portfolio.models import Portfolio, Holding, PortfolioNAV, HoldingReturn, BenchmarkReturn, MarketPrice
from portfolio.market_data import get_or_fetch_market_price, fetch_and_store_historical_prices
from decimal import Decimal
import logging
from datetime import date, timedelta
import pandas as pd
import yfinance as yf
from django.db.utils import OperationalError
import time
from ...models import Asset, GlobalMarketPrice

logger = logging.getLogger(__name__)

# Constants
INITIAL_NAV_VALUE = Decimal('100.0')
BENCHMARKS = {
    'SP500': '^GSPC',
    'MSCI_WORLD': 'URTH',  # iShares MSCI World ETF as proxy
    'GLOBAL_AGG': 'AGG'    # iShares Core U.S. Aggregate Bond ETF as proxy
}

class Command(BaseCommand):
    help = 'Update daily market prices and NAV for all portfolios'

    def handle(self, *args, **options):
        """Update daily market prices and NAV for all portfolios"""
        force = options.get('force', False)
        today = timezone.now().date()  # Use current date instead of hardcoded test date
        
        logger.info("Starting daily update process")
        
        # Process each portfolio
        portfolios = Portfolio.objects.all()
        for portfolio in portfolios:
            try:
                self.process_portfolio(portfolio, force, today)
            except Exception as e:
                logger.error(f"Error processing portfolio {portfolio.id}: {e}")
                continue
        
        # Update benchmark returns
        try:
            self.update_benchmark_returns(force, today)
        except Exception as e:
            logger.error(f"Error updating benchmark returns: {e}")
            
        logger.info("Daily update process complete")

    def process_portfolio(self, portfolio, force, today):
        """Process a single portfolio's NAV and returns"""
        logger.info(f"Processing portfolio {portfolio.id}")
        
        try:
            # Determine start date for backfill
            if force:
                start_date = Holding.objects.filter(portfolio=portfolio).aggregate(
                    Min('purchase_date')
                )['purchase_date__min']
                if not start_date:
                    logger.info(f"No holdings found for portfolio {portfolio.id}")
                    return
            else:
                latest_nav = PortfolioNAV.objects.filter(portfolio=portfolio).order_by('-date').first()
                if latest_nav:
                    start_date = latest_nav.date + timedelta(days=1)
                else:
                    start_date = Holding.objects.filter(portfolio=portfolio).aggregate(
                        Min('purchase_date')
                    )['purchase_date__min']
                    if not start_date:
                        logger.info(f"No holdings found for portfolio {portfolio.id}")
                        return
            
            # Skip if already up to date
            if not force and start_date > today:
                logger.info(f"Portfolio {portfolio.id} is already up to date")
                return
            
            logger.info(f"Processing portfolio {portfolio.id} from {start_date} to {today}")
            
            # Get all holdings that existed during the period
            holdings = Holding.objects.filter(
                Q(portfolio=portfolio) & 
                Q(purchase_date__lte=today) & 
                ~Q(asset_class='Cash')
            )
            
            # First, ensure we have all historical prices
            for holding in holdings:
                logger.info(f"Fetching historical prices for {holding.ticker} from {start_date} to {today}")
                success = fetch_and_store_historical_prices(holding.ticker, start_date, today)
                if not success:
                    logger.warning(f"Failed to fetch historical prices for {holding.ticker}")
            
            # Now process each date
            current_date = start_date
            prev_nav = None
            
            while current_date <= today:
                if current_date.weekday() >= 5:  # Skip weekends
                    current_date += timedelta(days=1)
                    continue
                
                # Calculate portfolio value
                total_value = Decimal('0')
                holding_updates = []
                active_holdings = holdings.filter(purchase_date__lte=current_date)
                
                for holding in active_holdings:
                    if holding.asset_class == 'Cash':
                        total_value += holding.book_cost
                        continue
                    
                    price = get_or_fetch_market_price(holding.ticker, current_date)
                    if price is None:
                        logger.warning(f"No price available for {holding.ticker} on {current_date}")
                        continue
                    
                    market_value = price * holding.quantity
                    total_value += market_value
                    
                    # Update holding's market value
                    holding.market_value = market_value
                    holding_updates.append(holding)
                
                # Calculate daily return
                daily_return = None
                if prev_nav is not None and prev_nav > 0:
                    daily_return = (total_value - prev_nav) / prev_nav
                
                # Create or update NAV record
                nav_obj, created = PortfolioNAV.objects.update_or_create(
                    portfolio=portfolio,
                    date=current_date,
                    defaults={
                        'total_value': total_value,
                        'nav': total_value,
                        'daily_return': daily_return,
                        'last_updated': timezone.now()
                    }
                )
                
                # Update holdings in bulk
                if holding_updates:
                    Holding.objects.bulk_update(holding_updates, ['market_value'])
                
                prev_nav = total_value
                current_date += timedelta(days=1)
            
            logger.info(f"Completed processing portfolio {portfolio.id}")
            
        except Exception as e:
            logger.error(f"Error processing portfolio {portfolio.id}: {e}")

    def update_benchmark_returns(self, force, today):
        """Update benchmark returns for all configured benchmarks"""
        logger.info("Updating benchmark returns")
        
        for name, ticker in BENCHMARKS.items():
            try:
                # Determine start date
                if force:
                    start_date = date(2019, 1, 1)  # 5 years of history
                else:
                    latest = BenchmarkReturn.objects.filter(
                        benchmark_name=name
                    ).order_by('-date').first()
                    
                    if latest:
                        start_date = latest.date + timedelta(days=1)
                    else:
                        start_date = date(2019, 1, 1)
                
                if start_date > today:
                    logger.info(f"Benchmark {name} is up to date")
                    continue
                
                logger.info(f"Fetching {name} data from {start_date} to {today}")
                
                # Fetch historical data
                benchmark = yf.Ticker(ticker)
                hist = benchmark.history(start=start_date, end=today + timedelta(days=1))
                
                if hist.empty:
                    logger.warning(f"No data available for benchmark {name}")
                    continue
                
                # Calculate daily returns
                returns = []
                prev_close = None
                
                for index, row in hist.iterrows():
                    current_date = index.date()
                    if current_date.weekday() >= 5:  # Skip weekends
                        continue
                        
                    close = Decimal(str(row['Close']))
                    
                    if prev_close:
                        daily_return = (close - prev_close) / prev_close
                        returns.append(
                            BenchmarkReturn(
                                benchmark_name=name,
                                date=current_date,
                                daily_return=daily_return
                            )
                        )
                    
                    prev_close = close
                
                # Bulk create new returns
                if returns:
                    BenchmarkReturn.objects.bulk_create(
                        returns,
                        ignore_conflicts=True
                    )
                    
                logger.info(f"Created {len(returns)} returns for benchmark {name}")
                
            except Exception as e:
                logger.error(f"Error updating benchmark {name}: {e}")

    def prefetch_prices(self, holdings, start_date, end_date):
        """Prefetch prices for all holdings in the date range"""
        for holding in holdings:
            try:
                success = fetch_and_store_historical_prices(holding.ticker, start_date, end_date)
                if not success:
                    logger.warning(f"Failed to prefetch prices for {holding.ticker}")
            except Exception as e:
                logger.error(f"Error prefetching prices for {holding.ticker}: {e}")

    def get_earliest_date(self, portfolio):
        """Get the earliest date to start updating from"""
        earliest_holding_date = portfolio.holdings.aggregate(
            Min('purchase_date')
        )['purchase_date__min']
        
        earliest_nav_date = portfolio.nav_records.aggregate(
            Min('date')
        )['date__min']
        
        if earliest_holding_date and earliest_nav_date:
            return min(earliest_holding_date, earliest_nav_date)
        elif earliest_holding_date:
            return earliest_holding_date
        elif earliest_nav_date:
            return earliest_nav_date
        else:
            return None

    def update_portfolio_prices(self, portfolio, current_date):
        """Update market prices for all holdings in a portfolio"""
        # Get active holdings for this date
        active_holdings = portfolio.holdings.filter(purchase_date__lte=current_date)
        
        for holding in active_holdings:
            if holding.asset_class == 'Cash':
                continue
            
            # Update market price
            price = get_or_fetch_market_price(holding.ticker, current_date)
            if price is None:
                logger.error(f"Could not get price for {holding.ticker} on {current_date}")
                continue
            
            # Update market value
            holding.market_value = price * holding.quantity
            holding.save()

    def update_portfolio_nav(self, portfolio, current_date):
        """Update NAV for a portfolio on a given date"""
        try:
            # Get active holdings for this date
            active_holdings = portfolio.holdings.filter(purchase_date__lte=current_date)
            
            # Calculate total value
            total_value = Decimal('0')
            for holding in active_holdings:
                if holding.market_value is None:
                    logger.error(f"No market value for holding {holding.id} on {current_date}")
                    continue
                total_value += holding.market_value
            
            # Create or update NAV record
            nav, created = PortfolioNAV.objects.update_or_create(
                portfolio=portfolio,
                date=current_date,
                defaults={
                    'total_value': total_value,
                    'nav': total_value,  # For now, just use total value as NAV
                    'last_updated': timezone.now()
                }
            )
            
            # Calculate daily return
            nav.daily_return = nav.calculate_daily_return()
            nav.save()
            
            return True
            
        except Exception as e:
            logger.error(f"Error updating NAV for portfolio {portfolio.id}: {e}")
            return False 