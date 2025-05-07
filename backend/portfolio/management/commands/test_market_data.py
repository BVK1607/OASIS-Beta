from django.core.management.base import BaseCommand
from django.utils import timezone
from datetime import datetime, date, timedelta
from decimal import Decimal
from portfolio.market_data import (
    get_or_fetch_market_price,
    get_or_fetch_fx_rate,
    YFinanceProvider,
    TwelveDataProvider,
    is_trading_day
)
from portfolio.models import Asset, GlobalMarketPrice, FXRate
import logging

logger = logging.getLogger(__name__)

class Command(BaseCommand):
    help = 'Test market data functionality with real API calls'

    def handle(self, *args, **options):
        self.stdout.write("Testing market data functionality...")
        
        # Find the most recent trading day
        test_date = datetime.now().date()
        while not is_trading_day(test_date):
            test_date = test_date - timedelta(days=1)
        
        self.stdout.write(f"Using test date: {test_date}")
        
        # Test stocks
        test_stocks = ['AAPL', 'MSFT', 'GOOGL', 'AMZN', 'TSLA']
        
        # Test YFinance provider
        self.stdout.write("\nTesting YFinance provider:")
        yf_provider = YFinanceProvider()
        
        for ticker in test_stocks:
            self.stdout.write(f"\nTesting {ticker}:")
            
            # Test single price fetch
            price = yf_provider.get_price(ticker, test_date)
            if price:
                self.stdout.write(self.style.SUCCESS(f"✓ Current price: {price}"))
            else:
                self.stdout.write(self.style.ERROR(f"✗ Failed to get price"))
            
            # Test historical prices
            start_date = test_date - timedelta(days=5)
            prices = yf_provider.get_historical_prices(ticker, start_date, test_date)
            if prices:
                self.stdout.write(self.style.SUCCESS(f"✓ Historical prices: {len(prices)} days"))
                for d, data in prices.items():
                    self.stdout.write(f"  {d}: {data}")
            else:
                self.stdout.write(self.style.ERROR(f"✗ Failed to get historical prices"))
        
        # Test market price storage and retrieval
        self.stdout.write("\nTesting market price storage and retrieval:")
        for ticker in test_stocks:
            price = get_or_fetch_market_price(ticker, test_date)
            if price:
                self.stdout.write(self.style.SUCCESS(f"✓ Stored/retrieved price for {ticker}: {price}"))
            else:
                self.stdout.write(self.style.ERROR(f"✗ Failed to store/retrieve price for {ticker}"))
        
        # Test FX rates
        self.stdout.write("\nTesting FX rates:")
        test_currencies = [
            ('USD', 'GBP'),
            ('USD', 'EUR'),
            ('GBP', 'EUR')
        ]
        
        for base, target in test_currencies:
            rate = get_or_fetch_fx_rate(base, target, test_date)
            if rate:
                self.stdout.write(self.style.SUCCESS(f"✓ {base}/{target} rate: {rate}"))
            else:
                self.stdout.write(self.style.ERROR(f"✗ Failed to get {base}/{target} rate"))
        
        # Check data quality
        self.stdout.write("\nChecking data quality:")
        for ticker in test_stocks:
            prices = GlobalMarketPrice.objects.filter(
                asset__ticker=ticker,
                date__gte=test_date - timedelta(days=5)
            ).order_by('date')
            
            if prices.exists():
                self.stdout.write(self.style.SUCCESS(f"✓ Found {prices.count()} prices for {ticker}"))
                
                # Check for price changes
                prev_price = None
                for price in prices:
                    if prev_price:
                        change = abs((price.price - prev_price) / prev_price)
                        if change > 0.5:  # 50% threshold
                            self.stdout.write(self.style.WARNING(
                                f"! Large price change for {ticker} on {price.date}: {change:.2%}"
                            ))
                    prev_price = price.price
            else:
                self.stdout.write(self.style.ERROR(f"✗ No prices found for {ticker}"))
        
        self.stdout.write(self.style.SUCCESS("\nMarket data test completed")) 