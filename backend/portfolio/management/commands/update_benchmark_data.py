from django.core.management.base import BaseCommand
from django.utils import timezone
from portfolio.models import BenchmarkReturn
from portfolio.market_data import get_market_price
from decimal import Decimal
import logging
from datetime import datetime, timedelta
import pytz

logger = logging.getLogger(__name__)

class Command(BaseCommand):
    help = 'Update benchmark returns for all portfolios'

    def handle(self, *args, **options):
        uk_tz = pytz.timezone('Europe/London')
        current_time = timezone.now().astimezone(uk_tz)
        
        # Run between 8:00 and 23:00 UK time
        if current_time.hour < 8 or current_time.hour >= 23:
            logger.info(f"Skipping benchmark update - current time {current_time.strftime('%H:%M')} is outside update window")
            return
            
        today = current_time.date()
        yesterday = today - timedelta(days=1)

        logger.info(f"Starting benchmark update at {current_time}")
        
        # List of benchmark tickers to track
        benchmark_tickers = ['^GSPC', '^IXIC', '^FTSE', '^N225']  # S&P 500, NASDAQ, FTSE 100, Nikkei 225
        
        for ticker in benchmark_tickers:
            try:
                # Get today's price
                today_data = get_market_price(ticker)
                
                # Get yesterday's price
                yesterday_data = get_market_price(ticker)
                
                if yesterday_data['price'] > 0:
                    daily_return = (today_data['price'] - yesterday_data['price']) / yesterday_data['price']
                    
                    # Update or create benchmark return record
                    BenchmarkReturn.objects.update_or_create(
                        ticker=ticker,
                        date=today,
                        defaults={
                            'daily_return': daily_return.quantize(Decimal('0.000001')),
                            'price': today_data['price']
                        }
                    )
                    
                    logger.info(f"Updated benchmark {ticker} return: {daily_return:.6f}")
                    
            except Exception as e:
                logger.error(f"Error updating benchmark {ticker}: {str(e)}")
                continue
                
        logger.info("Benchmark update completed") 