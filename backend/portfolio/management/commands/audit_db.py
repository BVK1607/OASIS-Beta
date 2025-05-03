from django.core.management.base import BaseCommand
from django.db import connection, models
from portfolio.models import Portfolio, Holding, MarketPrice, HoldingReturn, PortfolioNAV, TradeHistory
from django.db.models import Count, Q, F
from decimal import Decimal
import logging

logger = logging.getLogger(__name__)

class Command(BaseCommand):
    help = 'Audit database relationships and data integrity'

    def handle(self, *args, **options):
        self.stdout.write('Starting database audit...\n')

        # Check for orphaned market prices (no matching portfolio)
        self.stdout.write('\n1. Checking MarketPrice records...')
        null_portfolio_prices = MarketPrice.objects.filter(portfolio__isnull=True)
        self.stdout.write(f'- Found {null_portfolio_prices.count()} market prices with NULL portfolio')
        
        # Get unique tickers with null portfolios
        null_portfolio_tickers = null_portfolio_prices.values('ticker').distinct()
        self.stdout.write(f'- Unique tickers with NULL portfolio: {null_portfolio_tickers.count()}')
        for ticker in null_portfolio_tickers:
            self.stdout.write(f'  * {ticker["ticker"]}')

        # Check for market prices without matching holdings
        self.stdout.write('\n2. Checking MarketPrice-Holding relationships...')
        all_holding_tickers = set(Holding.objects.values_list('ticker', flat=True).distinct())
        all_price_tickers = set(MarketPrice.objects.values_list('ticker', flat=True).distinct())
        
        prices_without_holdings = all_price_tickers - all_holding_tickers
        self.stdout.write(f'- Found {len(prices_without_holdings)} tickers in MarketPrice without matching Holdings:')
        for ticker in prices_without_holdings:
            self.stdout.write(f'  * {ticker}')

        # Check for holdings without market prices
        holdings_without_prices = all_holding_tickers - all_price_tickers
        self.stdout.write(f'- Found {len(holdings_without_prices)} tickers in Holdings without any MarketPrice:')
        for ticker in holdings_without_prices:
            self.stdout.write(f'  * {ticker}')

        # Check for orphaned holding returns
        self.stdout.write('\n3. Checking HoldingReturn records...')
        orphaned_returns = HoldingReturn.objects.filter(
            Q(holding__isnull=True) | Q(portfolio__isnull=True)
        )
        self.stdout.write(f'- Found {orphaned_returns.count()} orphaned holding returns')

        # Check for inconsistent portfolio references
        self.stdout.write('\n4. Checking portfolio reference consistency...')
        inconsistent_returns = HoldingReturn.objects.filter(
            ~Q(portfolio=F('holding__portfolio'))
        )
        self.stdout.write(f'- Found {inconsistent_returns.count()} holding returns with mismatched portfolio references')

        # Check for orphaned NAVs
        self.stdout.write('\n5. Checking PortfolioNAV records...')
        orphaned_navs = PortfolioNAV.objects.filter(portfolio__isnull=True)
        self.stdout.write(f'- Found {orphaned_navs.count()} orphaned NAV records')

        # Check for trade history consistency
        self.stdout.write('\n6. Checking TradeHistory records...')
        orphaned_trades = TradeHistory.objects.filter(portfolio__isnull=True)
        self.stdout.write(f'- Found {orphaned_trades.count()} orphaned trade records')

        # Check for holdings with invalid quantities or prices
        self.stdout.write('\n7. Checking Holdings data validity...')
        invalid_holdings = Holding.objects.filter(
            Q(quantity__lt=0) |
            Q(book_cost__lt=0) |
            Q(market_value__lt=0)
        )
        self.stdout.write(f'- Found {invalid_holdings.count()} holdings with invalid quantities or prices')

        # Check for duplicate market prices
        self.stdout.write('\n8. Checking for duplicate market prices...')
        duplicates = MarketPrice.objects.values('ticker', 'date').annotate(
            count=Count('id')
        ).filter(count__gt=1)
        self.stdout.write(f'- Found {duplicates.count()} sets of duplicate market prices')

        # Check for future dates
        self.stdout.write('\n9. Checking for future dates...')
        from django.utils import timezone
        today = timezone.now().date()
        future_prices = MarketPrice.objects.filter(date__gt=today)
        future_returns = HoldingReturn.objects.filter(date__gt=today)
        future_navs = PortfolioNAV.objects.filter(date__gt=today)
        
        self.stdout.write(f'- Found {future_prices.count()} market prices with future dates')
        self.stdout.write(f'- Found {future_returns.count()} holding returns with future dates')
        self.stdout.write(f'- Found {future_navs.count()} NAV records with future dates')

        # Check for stale market prices
        self.stdout.write('\n10. Checking for stale data...')
        from datetime import timedelta
        week_ago = today - timedelta(days=7)
        stale_holdings = Holding.objects.filter(
            last_price_update__lt=week_ago,
            asset_class__ne='Cash'
        )
        self.stdout.write(f'- Found {stale_holdings.count()} holdings with stale prices (>1 week old)')

        self.stdout.write('\nDatabase audit complete.') 