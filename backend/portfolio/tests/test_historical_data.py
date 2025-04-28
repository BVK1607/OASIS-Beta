from django.test import TestCase
from django.urls import reverse
from rest_framework.test import APIClient
from rest_framework import status
from portfolio.models import Portfolio, NAVSnapshot, HoldingsReturnSnapshot
from decimal import Decimal
from datetime import date, timedelta, datetime

class HistoricalDataAPITests(TestCase):
    def setUp(self):
        self.client = APIClient()
        self.portfolio = Portfolio.objects.create(
            name='Test Portfolio',
            description='Test Description',
            risk_tolerance='Moderate'
        )
        
        # Create test NAV snapshots
        self.dates = [date.today() - timedelta(days=x) for x in range(10)]
        for d in self.dates:
            NAVSnapshot.objects.create(
                portfolio=self.portfolio,
                date=d,
                nav=Decimal('100.00'),
                daily_return=Decimal('0.01')
            )
        
        # Create test holdings returns
        self.tickers = ['AAPL', 'MSFT']
        for d in self.dates:
            for ticker in self.tickers:
                HoldingsReturnSnapshot.objects.create(
                    portfolio=self.portfolio,
                    ticker=ticker,
                    date=d,
                    daily_return=Decimal('0.02')
                )

    def test_nav_history_endpoint(self):
        """Test the NAV history endpoint with various filters"""
        url = reverse('portfolio-nav-history', args=[self.portfolio.id])
        
        # Test without filters
        response = self.client.get(url)
        self.assertEqual(response.status_code, status.HTTP_200_OK)
        self.assertEqual(len(response.data), 10)
        
        # Test with date filter
        start_date = (date.today() - timedelta(days=5)).strftime('%Y-%m-%d')
        response = self.client.get(f'{url}?start_date={start_date}')
        self.assertEqual(response.status_code, status.HTTP_200_OK)
        self.assertEqual(len(response.data), 6)  # 5 days + today
        
        # Verify response structure
        self.assertTrue(all(
            all(key in item for key in ['date', 'nav', 'daily_return'])
            for item in response.data
        ))

    def test_holdings_returns_endpoint(self):
        """Test the holdings returns endpoint with various filters"""
        url = reverse('portfolio-holdings-returns', args=[self.portfolio.id])
        
        # Test without filters
        response = self.client.get(url)
        self.assertEqual(response.status_code, status.HTTP_200_OK)
        self.assertEqual(len(response.data), len(self.tickers))
        
        # Test with ticker filter
        response = self.client.get(f'{url}?ticker=AAPL')
        self.assertEqual(response.status_code, status.HTTP_200_OK)
        self.assertEqual(len(response.data), 1)
        
        # Test with date filter
        start_date = (date.today() - timedelta(days=5)).strftime('%Y-%m-%d')
        response = self.client.get(f'{url}?start_date={start_date}')
        self.assertEqual(response.status_code, status.HTTP_200_OK)
        for ticker_data in response.data.values():
            self.assertEqual(len(ticker_data), 6)  # 5 days + today
        
        # Verify response structure
        self.assertTrue(all(
            isinstance(ticker_data, list) and
            all(isinstance(item, dict) and
                all(key in item for key in ['date', 'daily_return'])
                for item in ticker_data)
            for ticker_data in response.data.values()
        ))

    def test_data_consistency(self):
        """Test that the data is consistent across endpoints"""
        nav_url = reverse('portfolio-nav-history', args=[self.portfolio.id])
        returns_url = reverse('portfolio-holdings-returns', args=[self.portfolio.id])
        
        nav_response = self.client.get(nav_url)
        returns_response = self.client.get(returns_url)
        
        # Convert string dates to date objects for NAV data
        nav_dates = {
            datetime.strptime(item['date'], '%Y-%m-%d').date()
            for item in nav_response.data
        }
        
        # Get dates from holdings returns
        for ticker_data in returns_response.data.values():
            returns_dates = {
                datetime.strptime(item['date'], '%Y-%m-%d').date()
                if isinstance(item['date'], str)
                else item['date']
                for item in ticker_data
            }
            self.assertEqual(nav_dates, returns_dates) 