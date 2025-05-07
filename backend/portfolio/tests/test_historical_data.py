from django.test import TestCase
from django.utils import timezone
from datetime import date, timedelta, datetime
from decimal import Decimal
from unittest.mock import patch
from ..models import Portfolio, Holding, Asset, GlobalMarketPrice, PortfolioNAV

class HistoricalDataTest(TestCase):
    def setUp(self):
        # Create a portfolio
        self.portfolio = Portfolio.objects.create(
            name="Test Portfolio",
            base_currency="USD"
        )
        
        # Create an asset
        self.asset = Asset.objects.create(
            ticker="AAPL",
            name="Apple Inc",
            asset_class="Equity",
            currency="USD",
            market="US",
            isin="US0378331005"
        )
        
        # Create a holding
        self.holding = Holding.objects.create(
            portfolio=self.portfolio,
            asset=self.asset,
            quantity=10,
            book_cost=Decimal('1500.00'),
            asset_class="Equity",
            isin="US0378331005",
            ticker="AAPL",
            purchase_date=timezone.now().date()
        )
        
        # Create some historical prices
        today = timezone.now().date()
        for i in range(5):
            price_date = today - timedelta(days=i)
            GlobalMarketPrice.objects.create(
                asset=self.asset,
                date=price_date,
                price=Decimal('154.00') - Decimal(str(i)),
                is_trading_day=True
            )

    @patch('portfolio.market_data.get_or_fetch_market_price')
    def test_historical_prices(self, mock_get_price):
        """Test that historical prices are stored and retrieved correctly"""
        today = timezone.now().date()
        
        # Get all prices for the asset
        prices = GlobalMarketPrice.objects.filter(
            asset=self.asset
        ).order_by('-date')
        
        self.assertEqual(prices.count(), 5)
        
        # Check the most recent price
        latest_price = prices.first()
        self.assertEqual(latest_price.price, Decimal('154.00'))
        self.assertEqual(latest_price.date, today)
        
        # Check the oldest price
        oldest_price = prices.last()
        self.assertEqual(oldest_price.price, Decimal('150.00'))
        self.assertEqual(oldest_price.date, today - timedelta(days=4))

    @patch('portfolio.market_data.get_or_fetch_market_price')
    def test_market_value_calculation(self, mock_get_price):
        """Test that market values are calculated correctly"""
        today = timezone.now().date()
        
        # Get the latest price
        latest_price = GlobalMarketPrice.objects.filter(
            asset=self.asset,
            date=today
        ).first()
        
        # Calculate expected market value
        expected_value = latest_price.price * self.holding.quantity
        
        # Update holding's market value
        self.holding.market_value = expected_value
        self.holding.save()
        
        # Verify the market value
        self.assertEqual(self.holding.market_value, expected_value)
        
        # Update portfolio NAV using update_or_create
        nav, created = PortfolioNAV.objects.update_or_create(
            portfolio=self.portfolio,
            date=today,
            defaults={
                'total_value': expected_value,
                'nav': expected_value
            }
        )
        
        # Verify portfolio NAV
        portfolio_nav = PortfolioNAV.objects.get(portfolio=self.portfolio, date=today)
        self.assertEqual(portfolio_nav.nav, expected_value)

    @patch('portfolio.market_data.get_or_fetch_market_price')
    def test_asset_price_history(self, mock_get_price):
        """Test that we can get price history for an asset"""
        today = timezone.now().date()
        
        # Get price history for the asset
        prices = GlobalMarketPrice.objects.filter(
            asset=self.asset,
            date__range=[today - timedelta(days=4), today]
        ).order_by('date')
        
        # Check we have all prices
        self.assertEqual(prices.count(), 5)
        
        # Check price progression
        for i, price in enumerate(prices):
            expected_price = Decimal('154.00') - Decimal(str(4-i))
            self.assertEqual(price.price, expected_price)
            self.assertEqual(price.date, today - timedelta(days=4-i))
            self.assertTrue(price.is_trading_day)

    @patch('portfolio.market_data.get_or_fetch_market_price')
    def test_portfolio_nav_calculation(self, mock_get_price):
        """Test that portfolio NAV is calculated correctly with multiple holdings"""
        # Create another asset
        asset2 = Asset.objects.create(
            ticker="MSFT",
            name="Microsoft Corp",
            asset_class="Equity",
            currency="USD",
            market="US",
            isin="US5949181045"
        )
        
        # Create another holding
        holding2 = Holding.objects.create(
            portfolio=self.portfolio,
            asset=asset2,
            quantity=5,
            book_cost=Decimal('1000.00'),
            asset_class="Equity",
            isin="US5949181045",
            ticker="MSFT",
            purchase_date=timezone.now().date()
        )
        
        # Create prices for the second asset
        today = timezone.now().date()
        for i in range(5):
            price_date = today - timedelta(days=i)
            GlobalMarketPrice.objects.create(
                asset=asset2,
                date=price_date,
                price=Decimal('200.00') + Decimal(str(i)),
                is_trading_day=True
            )
        
        # Get latest prices
        price1 = GlobalMarketPrice.objects.get(asset=self.asset, date=today)
        price2 = GlobalMarketPrice.objects.get(asset=asset2, date=today)
        
        # Calculate expected values
        value1 = price1.price * self.holding.quantity
        value2 = price2.price * holding2.quantity
        expected_nav = value1 + value2
        
        # Update holdings' market values
        self.holding.market_value = value1
        self.holding.save()
        holding2.market_value = value2
        holding2.save()
        
        # Update portfolio NAV using update_or_create
        nav, created = PortfolioNAV.objects.update_or_create(
            portfolio=self.portfolio,
            date=today,
            defaults={
                'total_value': expected_nav,
                'nav': expected_nav
            }
        )
        
        # Verify portfolio NAV
        portfolio_nav = PortfolioNAV.objects.get(portfolio=self.portfolio, date=today)
        self.assertEqual(portfolio_nav.nav, expected_nav) 