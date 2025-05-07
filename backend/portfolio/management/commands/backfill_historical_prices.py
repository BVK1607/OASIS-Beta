from django.core.management.base import BaseCommand
from django.utils import timezone
from datetime import date, timedelta
from portfolio.models import Asset, GlobalMarketPrice, Holding
from portfolio.market_data import fetch_and_store_historical_prices
import logging
import pandas as pd

logger = logging.getLogger(__name__)

class Command(BaseCommand):
    help = 'Backfill missing historical prices for all assets'

    def handle(self, *args, **options):
        today = timezone.now().date()
        
        # Get all unique tickers from holdings and assets
        holding_tickers = set(Holding.objects.exclude(ticker='').values_list('ticker', flat=True))
        asset_tickers = set(Asset.objects.values_list('ticker', flat=True))
        all_tickers = holding_tickers.union(asset_tickers)
        
        logger.info(f"Found {len(all_tickers)} unique tickers to process")
        
        for ticker in all_tickers:
            try:
                # Get earliest date for this ticker
                earliest_date = None
                
                # Check holdings
                holding = Holding.objects.filter(ticker=ticker).order_by('purchase_date').first()
                if holding:
                    earliest_date = holding.purchase_date
                
                # Check existing prices
                earliest_price = GlobalMarketPrice.objects.filter(
                    asset__ticker=ticker
                ).order_by('date').first()
                
                if earliest_price:
                    if earliest_date is None or earliest_price.date < earliest_date:
                        earliest_date = earliest_price.date
                
                if earliest_date is None:
                    logger.warning(f"No date found for {ticker}, skipping")
                    continue
                
                # Get latest price date
                latest_price = GlobalMarketPrice.objects.filter(
                    asset__ticker=ticker
                ).order_by('-date').first()
                
                if latest_price:
                    # Check for gaps
                    all_dates = pd.date_range(earliest_date, latest_price.date, freq='D')
                    existing_dates = set(
                        GlobalMarketPrice.objects.filter(
                            asset__ticker=ticker,
                            date__range=[earliest_date, latest_price.date]
                        ).values_list('date', flat=True)
                    )
                    
                    missing_dates = [d.date() for d in all_dates if d.date() not in existing_dates]
                    
                    if missing_dates:
                        logger.info(f"Found {len(missing_dates)} missing dates for {ticker}")
                        # Fetch missing dates in chunks
                        chunk_size = 30
                        for i in range(0, len(missing_dates), chunk_size):
                            chunk = missing_dates[i:i + chunk_size]
                            start_date = min(chunk)
                            end_date = max(chunk)
                            success = fetch_and_store_historical_prices(ticker, start_date, end_date)
                            if not success:
                                logger.error(f"Failed to fetch prices for {ticker} from {start_date} to {end_date}")
                
                # Update to today
                if not latest_price or latest_price.date < today:
                    logger.info(f"Updating {ticker} from {latest_price.date if latest_price else earliest_date} to {today}")
                    success = fetch_and_store_historical_prices(
                        ticker,
                        latest_price.date + timedelta(days=1) if latest_price else earliest_date,
                        today
                    )
                    if not success:
                        logger.error(f"Failed to update prices for {ticker} to {today}")
                
            except Exception as e:
                logger.error(f"Error processing {ticker}: {e}")
                continue
                
        logger.info("Historical price backfill complete") 