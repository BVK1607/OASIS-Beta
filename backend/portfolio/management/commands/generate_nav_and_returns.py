import logging
from django.core.management.base import BaseCommand
from django.db import transaction
from django.utils import timezone
from datetime import timedelta, date
from decimal import Decimal
from portfolio.models import Portfolio, Holding, PortfolioNAV, HoldingReturn
from portfolio.market_data import get_historical_prices
import yfinance as yf

logger = logging.getLogger(__name__)

class Command(BaseCommand):
    help = 'Generate 5 years of historical NAV and holdings returns for all portfolios.'

    def add_arguments(self, parser):
        parser.add_argument('--today', action='store_true', help='Only calculate and save NAV for today')
        parser.add_argument('--force', action='store_true', help='Force regeneration of all data')
        parser.add_argument('--portfolio', type=int, help='Process only specific portfolio ID')

    def handle(self, *args, **options):
        portfolios = Portfolio.objects.all()
        
        for portfolio in portfolios:
            logger.info(f"Processing portfolio: {portfolio.name}")
            
            # Get all holdings for this portfolio
            holdings = portfolio.holdings.all()
            
            # Get the date range for NAV calculation
            start_date = holdings.aggregate(Min('trade_date'))['trade_date__min']
            if not start_date:
                logger.warning(f"No holdings found for portfolio {portfolio.name}")
                continue
                
            end_date = date.today()
            
            # Get all trading days first
            trading_days = set()
            for holding in holdings:
                if holding.ticker:  # Only for securities, not cash
                    try:
                        prices = get_historical_prices(holding.ticker, start_date, end_date)
                        if prices is not None and not prices.empty:
                            trading_days.update(prices.index.date)
                    except Exception as e:
                        logger.warning(f"Error fetching prices for {holding.ticker}: {e}")
                        continue
            
            trading_days = sorted(trading_days)
            if not trading_days:
                logger.warning(f"No trading days found for portfolio {portfolio.name}")
                continue
                
            # Delete existing NAV records for recalculation
            PortfolioNAV.objects.filter(portfolio=portfolio).delete()
            
            # Process each trading day
            for current_date in trading_days:
                total_value = Decimal('0')
                securities_processed = False
                
                # Calculate total value including both securities and cash
                for holding in holdings:
                    # Skip holdings that haven't been purchased yet
                    if holding.trade_date > current_date:
                        continue
                        
                    if holding.asset_class == 'Cash':
                        total_value += holding.book_cost
                        continue
                        
                    try:
                        market_data = get_market_price(holding.ticker, current_date)
                        if market_data and market_data['price']:
                            market_value = market_data['price'] * holding.quantity
                            total_value += market_value
                            securities_processed = True
                    except Exception as e:
                        logger.warning(f"Error getting price for {holding.ticker} on {current_date}: {str(e)}")
                        continue
                
                # Only create NAV record if we successfully processed securities
                if securities_processed:
                    # Get previous NAV for daily return calculation
                    prev_nav = PortfolioNAV.objects.filter(
                        portfolio=portfolio,
                        date__lt=current_date
                    ).order_by('-date').first()
                    
                    daily_return = None
                    if prev_nav and prev_nav.total_value > 0:
                        daily_return = ((total_value / prev_nav.total_value) - 1) * 100
                    
                    # Save NAV record
                    PortfolioNAV.objects.create(
                        portfolio=portfolio,
                        date=current_date,
                        nav=total_value.quantize(Decimal('0.0001')),
                        total_value=total_value.quantize(Decimal('0.0001')),
                        daily_return=daily_return.quantize(Decimal('0.000001')) if daily_return is not None else None
                    )
            
            logger.info(f"Completed NAV generation for portfolio {portfolio.name}")

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