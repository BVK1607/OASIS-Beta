from django.core.management.base import BaseCommand
from django.db.models import Sum, F, Q
from decimal import Decimal
import logging
from datetime import datetime, timedelta
from portfolio.models import Portfolio, Holding, PortfolioNAV, TradeHistory

logger = logging.getLogger(__name__)

class Command(BaseCommand):
    help = 'Checks data consistency across portfolio models'

    def handle(self, *args, **options):
        self.stdout.write('Starting data consistency check...')
        
        # Check each portfolio
        for portfolio in Portfolio.objects.all():
            self.stdout.write(f'\nChecking portfolio {portfolio.name} (ID: {portfolio.id})')
            
            # 1. Check holdings sum matches NAV
            holdings_total = portfolio.holdings.aggregate(
                total_market_value=Sum('market_value')
            )['total_market_value'] or Decimal('0')
            
            latest_nav = PortfolioNAV.objects.filter(
                portfolio=portfolio
            ).order_by('-date').first()
            
            if latest_nav:
                nav_diff = abs(latest_nav.total_value - holdings_total)
                if nav_diff > Decimal('0.01'):  # Allow small rounding differences
                    logger.error(
                        f"Portfolio {portfolio.id}: NAV mismatch. "
                        f"NAV: {latest_nav.total_value}, Holdings sum: {holdings_total}"
                    )
            
            # 2. Check for negative values
            negative_holdings = portfolio.holdings.filter(
                Q(quantity__lt=0) |
                Q(book_cost__lt=0) |
                Q(market_value__lt=0)
            )
            if negative_holdings.exists():
                logger.error(
                    f"Portfolio {portfolio.id}: Found {negative_holdings.count()} holdings with negative values"
                )
            
            # 3. Check for missing market prices
            missing_prices = portfolio.holdings.filter(
                Q(current_price__isnull=True) |
                Q(market_value__isnull=True)
            ).exclude(asset_class='Cash')
            if missing_prices.exists():
                logger.warning(
                    f"Portfolio {portfolio.id}: Found {missing_prices.count()} holdings with missing market prices"
                )
            
            # 4. Check trade history consistency
            trades = TradeHistory.objects.filter(portfolio=portfolio)
            for trade in trades:
                if trade.trade_type in ['Buy', 'Sell']:
                    holding = portfolio.holdings.filter(
                        ticker=trade.ticker,
                        trade_date__lte=trade.date
                    ).order_by('-trade_date').first()
                    
                    if holding:
                        expected_quantity = holding.quantity
                        if trade.trade_type == 'Buy':
                            expected_quantity += trade.quantity
                        else:
                            expected_quantity -= trade.quantity
                        
                        if expected_quantity < 0:
                            logger.error(
                                f"Portfolio {portfolio.id}: Trade {trade.id} would result in negative quantity"
                            )
            
            # 5. Check for duplicate holdings
            duplicate_holdings = portfolio.holdings.values(
                'isin', 'trade_date'
            ).annotate(
                count=models.Count('id')
            ).filter(count__gt=1)
            
            if duplicate_holdings.exists():
                logger.error(
                    f"Portfolio {portfolio.id}: Found duplicate holdings for ISINs: "
                    f"{', '.join(str(h['isin']) for h in duplicate_holdings)}"
                )
        
        self.stdout.write(self.style.SUCCESS('Data consistency check completed')) 