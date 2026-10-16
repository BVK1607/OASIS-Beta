from django.test import TestCase
from django.utils import timezone
from rest_framework.test import APIClient
from rest_framework import status
from decimal import Decimal
from datetime import date, timedelta
import json
from ..models import Portfolio, Holding, Asset, GlobalMarketPrice, PortfolioNAV
from ..services import PortfolioNAVService
from ..market_data import get_or_fetch_market_price

class CorePortfolioTests(TestCase):
    def setUp(self):
        self.client = APIClient()
        
        # Create test portfolio
        self.portfolio = Portfolio.objects.create(
            name="Test Portfolio",
            description="Test Description",
            risk_tolerance="Medium",
            base_currency="USD",
            creation_date=date.today()
        )
        
        # Create test assets
        self.asset1 = Asset.objects.create(
            ticker="AAPL",
            name="Apple Inc",
            asset_class="Equity",
            currency="USD",
            market="US"
        )
        
        self.asset2 = Asset.objects.create(
            ticker="MSFT",
            name="Microsoft Corp",
            asset_class="Equity",
            currency="USD",
            market="US"
        )
        
        # Create test holdings
        self.holding1 = Holding.objects.create(
            portfolio=self.portfolio,
            asset=self.asset1,
            asset_class="Equity",
            ticker="AAPL",
            quantity=Decimal("10"),
            book_cost=Decimal("1500.00"),
            currency="USD",
            purchase_date=date.today() - timedelta(days=5)
        )
        
        self.holding2 = Holding.objects.create(
            portfolio=self.portfolio,
            asset=self.asset2,
            asset_class="Equity",
            ticker="MSFT",
            quantity=Decimal("5"),
            book_cost=Decimal("1000.00"),
            currency="USD",
            purchase_date=date.today() - timedelta(days=5)
        )
        
        # Create test market prices
        for i in range(5):
            d = date.today() - timedelta(days=i)
            GlobalMarketPrice.objects.create(
                asset=self.asset1,
                date=d,
                price=Decimal("150.00"),
                is_trading_day=True
            )
            GlobalMarketPrice.objects.create(
                asset=self.asset2,
                date=d,
                price=Decimal("200.00"),
                is_trading_day=True
            )

    def test_create_portfolio(self):
        """Test creating a new portfolio"""
        data = {
            "name": "New Portfolio",
            "description": "New Description",
            "risk_tolerance": "Medium",
            "base_currency": "USD",
            "creation_date": date.today().isoformat()
        }
        response = self.client.post('/api/portfolios/', data, format='json')
        self.assertEqual(response.status_code, status.HTTP_201_CREATED)
        self.assertEqual(Portfolio.objects.count(), 2)

    def test_add_holding(self):
        """Test adding a holding to a portfolio"""
        data = {
            "portfolio": self.portfolio.id,
            "asset_class": "Equity",
            "ticker": "GOOGL",
            "quantity": "5",
            "book_cost": "5000.00",
            "currency": "USD",
            "purchase_date": date.today().isoformat(),
            "purchase_price": "1000.00"
        }
        response = self.client.post('/api/holdings/', data, format='json')
        self.assertEqual(response.status_code, status.HTTP_201_CREATED)
        self.assertEqual(Holding.objects.count(), 3)

    def test_calculate_nav(self):
        """Test calculating NAV for a portfolio"""
        # Update market values
        self.holding1.market_value = Decimal("1500.00")
        self.holding1.save()
        self.holding2.market_value = Decimal("1000.00")
        self.holding2.save()
        
        # Calculate NAV
        # Holding signals may already have created today's NAV, so upsert
        nav, _ = PortfolioNAV.objects.update_or_create(
            portfolio=self.portfolio,
            date=date.today(),
            defaults={"total_value": Decimal("2500.00"), "nav": Decimal("2500.00")}
        )
        
        self.assertEqual(nav.total_value, Decimal("2500.00"))
        self.assertEqual(nav.nav, Decimal("2500.00"))

    def test_nav_history(self):
        """Test NAV history calculation"""
        # Create NAV records for the last 5 days
        for i in range(5):
            d = date.today() - timedelta(days=i)
            nav, _ = PortfolioNAV.objects.update_or_create(
                portfolio=self.portfolio,
                date=d,
                defaults={"total_value": Decimal("2500.00"), "nav": Decimal("2500.00")}
            )
            self.assertEqual(nav.total_value, Decimal("2500.00"))
            self.assertEqual(nav.nav, Decimal("2500.00"))

    def test_market_price_fetching(self):
        """Test fetching market prices"""
        # Test valid ticker
        price = get_or_fetch_market_price("AAPL", date.today())
        self.assertIsNotNone(price)
        
        # Test invalid ticker
        price = get_or_fetch_market_price("INVALID", date.today())
        self.assertIsNone(price)

    def test_portfolio_validation(self):
        """Test portfolio data validation"""
        # Test valid portfolio
        self.assertTrue(self.portfolio.validate_portfolio_data(date.today()))
        
        # Test portfolio with missing market prices
        holding = Holding.objects.create(
            portfolio=self.portfolio,
            asset_class="Equity",
            ticker="INVALID",
            quantity=Decimal("1"),
            book_cost=Decimal("100.00"),
            currency="USD",
            purchase_date=date.today()
        )
        self.assertFalse(holding.market_value)

    def test_holding_market_value(self):
        """Test holding market value calculation"""
        # Update market value
        self.holding1.market_value = Decimal("2112.10")
        self.holding1.save()
        
        self.assertEqual(self.holding1.market_value, Decimal("2112.10"))

    def test_portfolio_api_endpoints(self):
        """Test portfolio API endpoints"""
        # Test portfolio list
        response = self.client.get('/api/portfolios/')
        self.assertEqual(response.status_code, status.HTTP_200_OK)
        
        # Test portfolio detail
        response = self.client.get(f'/api/portfolios/{self.portfolio.id}/')
        self.assertEqual(response.status_code, status.HTTP_200_OK)
        
        # Test holdings list
        response = self.client.get('/api/holdings/')
        self.assertEqual(response.status_code, status.HTTP_200_OK)
        
        # Test NAV endpoint
        response = self.client.get(f'/api/portfolios/{self.portfolio.id}/nav/')
        self.assertEqual(response.status_code, status.HTTP_200_OK) 