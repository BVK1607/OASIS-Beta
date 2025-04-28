import logging
from django.core.management.base import BaseCommand
from django.db import transaction
from django.utils import timezone
from datetime import timedelta, date
from decimal import Decimal
from portfolio.models import Portfolio, Holding, PortfolioNAV, HoldingReturn
from portfolio.market_data import get_historical_prices

logger = logging.getLogger(__name__)

class Command(BaseCommand):
    help = 'Generate 5 years of historical NAV and holdings returns for all portfolios.'

    def add_arguments(self, parser):
        parser.add_argument('--today', action='store_true', help='Only calculate and save NAV for today')

    def handle(self, *args, **options):
        today = timezone.now().date()
        if options.get('today'):
            start_date = today
        else:
            start_date = today - timedelta(days=5*365)
        for portfolio in Portfolio.objects.all():
            self.stdout.write(f"Processing portfolio: {portfolio.name}")
            try:
                self.generate_portfolio_history(portfolio, start_date, today)
            except Exception as e:
                logger.error(f"Error processing portfolio {portfolio.id}: {e}")
                continue

    def generate_portfolio_history(self, portfolio, start_date, end_date):
        holdings = list(portfolio.holdings.all())
        if not holdings:
            logger.info(f"No holdings for portfolio {portfolio.id}")
            return
        ticker_to_holding = {h.ticker: h for h in holdings if h.ticker}
        price_data = {}
        for holding in holdings:
            if not holding.ticker:
                continue
            holding_start = max(start_date, holding.trade_date)
            try:
                prices = get_historical_prices(holding.ticker, holding_start, end_date)
                price_data[holding.ticker] = prices
            except Exception as e:
                logger.error(f"Error fetching prices for {holding.ticker}: {e}")
                continue
        all_dates = set()
        for prices in price_data.values():
            all_dates.update(prices.index.date)
        all_dates = sorted(d for d in all_dates if d >= start_date and d <= end_date)
        if not all_dates:
            logger.info(f"No price data for portfolio {portfolio.id}")
            return
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
        PortfolioNAV.objects.filter(portfolio=portfolio, date__range=(all_dates[0], all_dates[-1])).delete()
        HoldingReturn.objects.filter(portfolio=portfolio, date__range=(all_dates[0], all_dates[-1])).delete()
        with transaction.atomic():
            PortfolioNAV.objects.bulk_create(nav_snapshots, batch_size=500)
            HoldingReturn.objects.bulk_create(holding_return_snapshots, batch_size=1000)
        logger.info(f"Inserted {len(nav_snapshots)} NAV and {len(holding_return_snapshots)} holding return snapshots for portfolio {portfolio.id}") 