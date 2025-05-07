from django.core.management.base import BaseCommand
from django.utils import timezone
from datetime import date
from portfolio.models import Holding
from portfolio.market_data import fetch_and_store_historical_prices
import logging

logger = logging.getLogger(__name__)

class Command(BaseCommand):
    help = 'Fetch and store historical prices for all holdings from earliest purchase date to today.'

    def handle(self, *args, **options):
        today = timezone.now().date()
        tickers = set(Holding.objects.values_list('ticker', flat=True))
        holdings = Holding.objects.all()
        ticker_dates = {}
        for holding in holdings:
            if holding.ticker not in ticker_dates:
                ticker_dates[holding.ticker] = holding.purchase_date
            else:
                ticker_dates[holding.ticker] = min(ticker_dates[holding.ticker], holding.purchase_date)
        for ticker, start_date in ticker_dates.items():
            self.stdout.write(f"Fetching prices for {ticker} from {start_date} to {today}")
            try:
                fetch_and_store_historical_prices(ticker, start_date, today)
            except Exception as e:
                logger.error(f"Failed to fetch prices for {ticker}: {e}")
        self.stdout.write(self.style.SUCCESS('Finished fetching historical prices for all holdings.')) 