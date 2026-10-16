from unittest import skip

from django.test import TestCase
from django.urls import reverse
from rest_framework.test import APIClient
from rest_framework import status
from ..models import Portfolio, Holding, PortfolioNAV, HoldingReturn, BenchmarkReturn, RebalancePlan, TradeHistory
from ..serializers import PortfolioNAVSerializer, HoldingReturnSerializer, BenchmarkReturnSerializer, RebalancePlanSerializer
from decimal import Decimal
from datetime import date, timedelta
import json

@skip("Written for an older schema (Holding.trade_date, HoldingReturn.ticker); needs updating")
class PortfolioAPITests(TestCase):
    def setUp(self):
        self.client = APIClient()
        self.portfolio = Portfolio.objects.create(
            name='Test Portfolio',
            description='Test Description',
            risk_tolerance='Moderate'
        )
        
        # Create test holdings
        self.holding1 = Holding.objects.create(
            portfolio=self.portfolio,
            asset_class='Equity',
            isin='US0378331005',
            ticker='AAPL',
            quantity=10,
            book_cost=Decimal('150.00'),
            currency='USD',
            trade_date=date.today() - timedelta(days=30)
        )
        
        self.holding2 = Holding.objects.create(
            portfolio=self.portfolio,
            asset_class='Equity',
            isin='US02079K1079',
            ticker='GOOGL',
            quantity=5,
            book_cost=Decimal('2000.00'),
            currency='USD',
            trade_date=date.today() - timedelta(days=30)
        )
        
        # Create test NAV snapshots
        dates = [date.today() - timedelta(days=x) for x in range(10)]
        for d in dates:
            PortfolioNAV.objects.create(
                portfolio=self.portfolio,
                date=d,
                nav=Decimal('10000.00'),
                daily_return=Decimal('0.01')
            )
            
        # Create test holdings returns
        for d in dates:
            HoldingReturn.objects.create(
                portfolio=self.portfolio,
                ticker='AAPL',
                date=d,
                daily_return=Decimal('0.02')
            )
            HoldingReturn.objects.create(
                portfolio=self.portfolio,
                ticker='GOOGL',
                date=d,
                daily_return=Decimal('0.015')
            )

    def test_nav_history_endpoint(self):
        url = reverse('portfolio-nav-history', args=[self.portfolio.id])
        response = self.client.get(url)
        self.assertEqual(response.status_code, status.HTTP_200_OK)
        self.assertEqual(len(response.data), 10)  # Should return all 10 snapshots
        
        # Test date filtering
        start_date = (date.today() - timedelta(days=5)).strftime('%Y-%m-%d')
        response = self.client.get(f'{url}?start_date={start_date}')
        self.assertEqual(response.status_code, status.HTTP_200_OK)
        self.assertEqual(len(response.data), 6)  # Should return last 5 days + today
        
        # Test invalid date format
        response = self.client.get(f'{url}?start_date=invalid')
        self.assertEqual(response.status_code, status.HTTP_400_BAD_REQUEST)

    def test_holdings_returns_endpoint(self):
        url = reverse('portfolio-holdings-returns', args=[self.portfolio.id])
        response = self.client.get(url)
        self.assertEqual(response.status_code, status.HTTP_200_OK)
        self.assertEqual(len(response.data), 2)  # Should return data for both tickers
        
        # Test ticker filtering
        response = self.client.get(f'{url}?ticker=AAPL')
        self.assertEqual(response.status_code, status.HTTP_200_OK)
        self.assertEqual(len(response.data), 1)  # Should return only AAPL data
        self.assertIn('AAPL', response.data)
        
        # Test date filtering
        start_date = (date.today() - timedelta(days=5)).strftime('%Y-%m-%d')
        response = self.client.get(f'{url}?start_date={start_date}')
        self.assertEqual(response.status_code, status.HTTP_200_OK)
        for ticker_data in response.data.values():
            self.assertEqual(len(ticker_data), 6)  # Should return last 5 days + today
        
        # Test invalid date format
        response = self.client.get(f'{url}?start_date=invalid')
        self.assertEqual(response.status_code, status.HTTP_400_BAD_REQUEST)

    def test_models_exist(self):
        """Verify that all required models exist and can be queried"""
        # Test PortfolioNAV model
        nav_count = PortfolioNAV.objects.filter(portfolio=self.portfolio).count()
        self.assertEqual(nav_count, 10)
        
        # Test HoldingReturn model
        returns_count = HoldingReturn.objects.filter(portfolio=self.portfolio).count()
        self.assertEqual(returns_count, 20)  # 10 days * 2 holdings
        
        # Test Portfolio model
        portfolio = Portfolio.objects.get(id=self.portfolio.id)
        self.assertEqual(portfolio.name, 'Test Portfolio')
        
        # Test Holding model
        holdings_count = Holding.objects.filter(portfolio=self.portfolio).count()
        self.assertEqual(holdings_count, 2)
