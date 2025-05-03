from django.core.management.base import BaseCommand
from django.utils import timezone
from django.db import transaction
from django.db.models import Min, Max
from portfolio.models import Portfolio, Holding, PortfolioNAV, HoldingReturn, BenchmarkReturn, MarketPrice
from portfolio.market_data import get_or_fetch_market_price, fetch_and_store_historical_prices
from decimal import Decimal
import logging
from datetime import date, timedelta
import pandas as pd
import yfinance as yf
from django.db.utils import OperationalError
import time

logger = logging.getLogger(__name__)

# Constants
INITIAL_NAV_VALUE = Decimal('100.0')
BENCHMARKS = {
    'SP500': '^GSPC',
    'MSCI_WORLD': 'URTH',  # iShares MSCI World ETF as proxy
    'GLOBAL_AGG': 'AGG'    # iShares Core U.S. Aggregate Bond ETF as proxy
}

class Command(BaseCommand):
    help = 'Update and backfill NAV, holdings returns, and benchmark returns'

    def add_arguments(self, parser):
        parser.add_argument(
            '--force',
            action='store_true',
            help='Force update even if data already exists'
        )

    def handle(self, *args, **options):
        force = options.get('force', False)
        today = timezone.now().date()
        
        logger.info(f"Starting daily update for {today}")
        
        # Process each portfolio
        for portfolio in Portfolio.objects.all():
            try:
                self.process_portfolio(portfolio, force, today)
            except Exception as e:
                logger.error(f"Error processing portfolio {portfolio.id}: {e}")
                continue
                
        logger.info("Daily update completed")

    def process_portfolio(self, portfolio, force, today):
        """Process a single portfolio's NAV and returns"""
        logger.info(f"Processing portfolio {portfolio.id}")
        
        try:
            # Determine start date for backfill
            if force:
                start_date = Holding.objects.filter(portfolio=portfolio).aggregate(
                    Min('trade_date')
                )['trade_date__min']
                if not start_date:
                    logger.info(f"No holdings found for portfolio {portfolio.id}")
                    return
            else:
                latest_nav = PortfolioNAV.objects.filter(portfolio=portfolio).order_by('-date').first()
                if latest_nav:
                    start_date = latest_nav.date + timedelta(days=1)
                else:
                    start_date = Holding.objects.filter(portfolio=portfolio).aggregate(
                        Min('trade_date')
                    )['trade_date__min']
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
                portfolio=portfolio,
                trade_date__lte=today
            ).exclude(asset_class='Cash')
            
            # Prefetch all required historical prices upfront
            self.prefetch_prices(holdings, start_date, today)
            
            # Get first NAV for indexing
            first_nav = None
            
            # Process each date
            current_date = start_date
            prev_nav = None
            
            while current_date <= today:
                if current_date.weekday() >= 5:  # Skip weekends
                    current_date += timedelta(days=1)
                    continue
                    
                # Calculate portfolio value outside transaction
                total_value = Decimal('0')
                holding_updates = []  # Store updates to apply in transaction
                active_holdings = holdings.filter(trade_date__lte=current_date)
                
                for holding in active_holdings:
                    try:
                        market_data = get_or_fetch_market_price(holding.ticker, current_date)
                        if not market_data or not market_data.get('price'):
                            continue
                            
                        price = market_data['price']
                        market_value = price * holding.quantity
                        total_value += market_value
                        
                        # Prepare holding return data
                        holding_updates.append({
                            'holding': holding,
                            'market_value': market_value,
                            'price': price
                        })
                        
                    except Exception as e:
                        logger.error(f"Error processing holding {holding.ticker} on {current_date}: {e}")
                        continue
                
                # Add cash positions
                cash_holdings = portfolio.holdings.filter(
                    asset_class='Cash',
                    trade_date__lte=current_date
                )
                for cash in cash_holdings:
                    total_value += cash.book_cost
                
                # Calculate NAV and returns
                if not first_nav:
                    first_nav = total_value
                    nav_value = Decimal('100.0')
                    daily_return = None
                else:
                    nav_value = Decimal('100.0') * (total_value / first_nav)
                    if prev_nav and prev_nav.total_value > 0:
                        daily_return = ((total_value / prev_nav.total_value) - 1) * 100
                    else:
                        daily_return = None
                
                # Apply updates in a single transaction
                max_retries = 3
                retry_delay = 1
                
                for attempt in range(max_retries):
                    try:
                        with transaction.atomic():
                            # Create NAV entry
                            nav = PortfolioNAV.objects.create(
                                portfolio=portfolio,
                                date=current_date,
                                total_value=total_value,
                                nav=nav_value,
                                daily_return=daily_return
                            )
                            prev_nav = nav
                            
                            # Bulk create holding returns
                            holding_returns = [
                                HoldingReturn(
                                    portfolio=portfolio,
                                    holding=update['holding'],
                                    date=current_date,
                                    market_value=update['market_value']
                                ) for update in holding_updates
                            ]
                            
                            if holding_returns:
                                HoldingReturn.objects.bulk_create(holding_returns)
                            
                            logger.info(f"Updated NAV for portfolio {portfolio.id} on {current_date}: TV={total_value}, NAV={nav_value}")
                            break
                            
                    except OperationalError as e:
                        if attempt == max_retries - 1:
                            logger.error(f"Failed to update after {max_retries} attempts on {current_date}: {e}")
                            raise
                        time.sleep(retry_delay)
                        retry_delay *= 2  # Exponential backoff
                        
                current_date += timedelta(days=1)
                
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