import logging
from django.core.management.base import BaseCommand
from django.db import transaction
from datetime import datetime, timedelta
from decimal import Decimal
import pandas as pd
import yfinance as yf
from portfolio.models import BenchmarkReturn

BENCHMARKS = [
    {"ticker": "^GSPC", "display_name": "S&P 500"},
    {"ticker": "URTH", "display_name": "MSCI World"},
    {"ticker": "AGG", "display_name": "Global Aggregate Bond"},
]

logger = logging.getLogger(__name__)

class Command(BaseCommand):
    help = 'Fetch and store 5 years of daily benchmark data (close, return, return index) for major indices.'

    def add_arguments(self, parser):
        parser.add_argument('--overwrite', action='store_true', help='Overwrite existing benchmark data')

    def handle(self, *args, **options):
        end_date = datetime.today().date()
        start_date = end_date - timedelta(days=5*365)
        for bm in BENCHMARKS:
            ticker = bm["ticker"]
            display_name = bm["display_name"]
            self.stdout.write(f"Fetching data for {display_name} ({ticker})...")
            try:
                ticker_obj = yf.Ticker(ticker)
                hist = ticker_obj.history(start=start_date, end=end_date)
                if hist.empty or 'Close' not in hist:
                    logger.error(f"No data or no 'Close' for {ticker}")
                    continue
                close_series = hist['Close']
                df = close_series.to_frame().asfreq('B')
                df['Close'] = df['Close'].ffill()
                df = df.dropna(subset=['Close'])
                df['daily_return'] = df['Close'].pct_change().fillna(0)
                df['return_index'] = (1 + df['daily_return']).cumprod() * 100
                df = df.reset_index()
                if options['overwrite']:
                    BenchmarkReturn.objects.filter(benchmark_name=ticker).delete()
                objs = []
                for _, row in df.iterrows():
                    objs.append(BenchmarkReturn(
                        benchmark_name=ticker,
                        display_name=display_name,
                        date=row['Date'].date() if isinstance(row['Date'], pd.Timestamp) else row['Date'],
                        close_price=Decimal(str(row['Close'])),
                        daily_return=Decimal(str(row['daily_return'])),
                        return_index=Decimal(str(row['return_index'])),
                    ))
                with transaction.atomic():
                    BenchmarkReturn.objects.bulk_create(objs, batch_size=1000)
                self.stdout.write(self.style.SUCCESS(f"Inserted {len(objs)} rows for {display_name}"))
            except Exception as e:
                logger.error(f"Error fetching {ticker}: {e}")
                self.stdout.write(self.style.ERROR(f"Failed to fetch {display_name}: {e}")) 