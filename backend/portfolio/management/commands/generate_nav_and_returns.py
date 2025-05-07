import logging
from django.core.management.base import BaseCommand
from django.db import transaction
from django.utils import timezone
from datetime import timedelta, date
from decimal import Decimal
from portfolio.models import Portfolio, Holding, PortfolioNAV, HoldingReturn
from portfolio.market_data import get_historical_prices
import yfinance as yf
import pandas as pd

logger = logging.getLogger(__name__)

class Command(BaseCommand):
    help = 'Generate 5 years of historical NAV and holdings returns for all portfolios.'

    def add_arguments(self, parser):
        parser.add_argument('--today', action='store_true', help='Only calculate and save NAV for today')
        parser.add_argument('--force', action='store_true', help='Force regeneration of all data')
        parser.add_argument('--portfolio', type=int, help='Process only specific portfolio ID')

    def handle(self, *args, **options):
        """Generate NAV and returns for all portfolios"""
        portfolios = Portfolio.objects.all()
        
        for portfolio in portfolios:
            logger.info(f"Processing portfolio {portfolio.id}")
            
            # Get the earliest holding date
            start_date = portfolio.holdings.aggregate(
                Min('purchase_date')
            )['purchase_date__min']
            
            if not start_date:
                logger.warning(f"No holdings found for portfolio {portfolio.id}")
                continue
                
            # Get all dates from start_date to today
            dates = pd.date_range(start=start_date, end=date.today(), freq='D')
            
            for d in dates:
                d = d.date()
                logger.info(f"Processing date {d}")
                
                # Get active holdings for this date
                holdings = portfolio.holdings.filter(purchase_date__lte=d)
                
                # Calculate total value
                total_value = Decimal('0')
                for holding in holdings:
                    if holding.asset_class == 'Cash':
                        total_value += holding.book_cost
                    else:
                        price = get_or_fetch_market_price(holding.ticker, d)
                        if price is None:
                            logger.error(f"Could not get price for {holding.ticker} on {d}")
                            continue
                        total_value += price * holding.quantity
                
                # Create or update NAV record
                nav, created = PortfolioNAV.objects.update_or_create(
                    portfolio=portfolio,
                    date=d,
                    defaults={
                        'total_value': total_value,
                        'nav': total_value,  # For now, just use total value as NAV
                        'last_updated': timezone.now()
                    }
                )
                
                # Calculate daily return
                nav.daily_return = nav.calculate_daily_return()
                nav.save()
                
                # Update holding returns
                for holding in holdings:
                    if holding.purchase_date > d:
                        continue
                        
                    if holding.asset_class == 'Cash':
                        value = holding.book_cost
                    else:
                        price = get_or_fetch_market_price(holding.ticker, d)
                        if price is None:
                            logger.error(f"Could not get price for {holding.ticker} on {d}")
                            continue
                        value = price * holding.quantity
                    
                    # Create or update holding return
                    HoldingReturn.objects.update_or_create(
                        holding=holding,
                        date=d,
                        defaults={
                            'market_value': value,
                            'last_updated': timezone.now()
                        }
                    )
            
            logger.info(f"Completed processing portfolio {portfolio.id}")
        
        self.stdout.write(self.style.SUCCESS('Successfully generated NAV and returns'))

    def generate_portfolio_history(self, portfolio, start_date, end_date, force=False):
        holdings = list(portfolio.holdings.all())
        if not holdings:
            logger.info(f"No holdings for portfolio {portfolio.id}")
            return
            
        logger.info(f"Processing {len(holdings)} holdings for portfolio {portfolio.id}")
        ticker_to_holding = {h.ticker: h for h in holdings if h.ticker}
        price_data = {}
        
        # Fetch historical prices for each holding
        for holding in holdings:
            if not holding.ticker:
                continue
                
            holding_start = max(start_date, holding.trade_date)
            try:
                logger.info(f"Fetching prices for {holding.ticker} from {holding_start} to {end_date}")
                prices = get_historical_prices(holding.ticker, holding_start, end_date)
                if prices is not None and not prices.empty:
                    price_data[holding.ticker] = prices
                    logger.info(f"Successfully fetched {len(prices)} prices for {holding.ticker}")
                else:
                    logger.warning(f"No price data available for {holding.ticker}")
            except Exception as e:
                logger.error(f"Error fetching prices for {holding.ticker}: {e}")
                continue
                
        if not price_data:
            logger.warning(f"No price data available for any holdings in portfolio {portfolio.id}")
            return
            
        # Get all unique dates
        all_dates = set()
        for prices in price_data.values():
            all_dates.update(prices.index.date)
        all_dates = sorted(d for d in all_dates if d >= start_date and d <= end_date)
        
        if not all_dates:
            logger.warning(f"No valid dates found for portfolio {portfolio.id}")
            return
            
        logger.info(f"Processing {len(all_dates)} dates for portfolio {portfolio.id}")
        
        # Generate NAV and returns
        nav_snapshots = []
        holding_return_snapshots = []
        prev_nav = None
        nav_base = None
        
        for d in all_dates:
            total_value = Decimal('0')
            for holding in holdings:
                if not holding.ticker:
                    if holding.asset_class == 'Cash' and holding.trade_date <= d:
                        total_value += holding.book_cost
                    continue
                    
                prices = price_data.get(holding.ticker)
                if prices is not None and d in prices.index.date and holding.trade_date <= d:
                    price = Decimal(str(prices.loc[prices.index.date == d].iloc[0]))
                    total_value += price * holding.quantity
                    
            if nav_base is None:
                nav_base = total_value if total_value > 0 else Decimal('1')
                
            nav = (total_value / nav_base) * Decimal('100')
            daily_return = None
            if prev_nav is not None and prev_nav > 0:
                daily_return = (nav - prev_nav) / prev_nav
                
            nav_snapshots.append(PortfolioNAV(
                portfolio=portfolio,
                date=d,
                nav=nav.quantize(Decimal('0.0001')),
                daily_return=daily_return.quantize(Decimal('0.000001')) if daily_return is not None else None
            ))
            prev_nav = nav
            
        # Generate holding returns
        for ticker, prices in price_data.items():
            prev_price = None
            for d in all_dates:
                if d not in prices.index.date:
                    continue
                    
                price = Decimal(str(prices.loc[prices.index.date == d].iloc[0]))
                daily_return = None
                if prev_price is not None and prev_price > 0:
                    daily_return = (price - prev_price) / prev_price
                    
                if daily_return is not None:
                    holding_return_snapshots.append(HoldingReturn(
                        portfolio=portfolio,
                        ticker=ticker,
                        date=d,
                        daily_return=daily_return.quantize(Decimal('0.000001'))
                    ))
                prev_price = price
                
        # Delete existing data if force is True
        if force:
            PortfolioNAV.objects.filter(portfolio=portfolio, date__range=(all_dates[0], all_dates[-1])).delete()
            HoldingReturn.objects.filter(portfolio=portfolio, date__range=(all_dates[0], all_dates[-1])).delete()
            
        # Bulk create new data
        with transaction.atomic():
            PortfolioNAV.objects.bulk_create(nav_snapshots, batch_size=500, ignore_conflicts=True)
            HoldingReturn.objects.bulk_create(holding_return_snapshots, batch_size=1000, ignore_conflicts=True)
            
        logger.info(f"Inserted {len(nav_snapshots)} NAV and {len(holding_return_snapshots)} holding return snapshots for portfolio {portfolio.id}") 