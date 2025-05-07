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
            changes = self.check_trade_history(portfolio, datetime.now().date())
            if changes:
                logger.error(f"Portfolio {portfolio.id}: Trade history inconsistencies:")
                for change in changes:
                    logger.error(change)
            
            # 5. Check for duplicate holdings
            duplicate_holdings = portfolio.holdings.values(
                'isin', 'purchase_date'
            ).annotate(
                count=models.Count('id')
            ).filter(count__gt=1)
            
            if duplicate_holdings.exists():
                logger.error(
                    f"Portfolio {portfolio.id}: Found duplicate holdings for ISINs: "
                    f"{', '.join(str(h['isin']) for h in duplicate_holdings)}"
                )
        
        self.stdout.write(self.style.SUCCESS('Data consistency check completed')) 

    def check_trade_history(self, portfolio, date):
        """Check trade history for a portfolio on a given date"""
        changes = []
        
        # Get all trades for this date
        trades = TradeHistory.objects.filter(
            portfolio=portfolio,
            date=date
        )
        
        for trade in trades:
            # Get the holding as it was at the time of the trade
            holding = Holding.objects.filter(
                portfolio=portfolio,
                purchase_date__lte=trade.date
            ).order_by('-purchase_date').first()
            
            if not holding:
                changes.append(f"No holding found for trade {trade.id}")
                continue
            
            # Check trade details match holding
            fields_to_check = [
                'portfolio', 'asset', 'quantity', 'book_cost',
                'isin', 'purchase_date'
            ]
            
            for field in fields_to_check:
                trade_value = getattr(trade, field)
                holding_value = getattr(holding, field)
                if trade_value != holding_value:
                    changes.append(
                        f"Trade {trade.id} {field} mismatch: "
                        f"trade={trade_value}, holding={holding_value}"
                    )
        
        return changes 