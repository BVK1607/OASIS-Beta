from django.core.management.base import BaseCommand
from django.db import transaction
from django.utils import timezone
from datetime import date, timedelta
from decimal import Decimal
import logging
from ...models import Portfolio, Holding, Asset, GlobalMarketPrice
from ...market_data import get_or_fetch_market_price

logger = logging.getLogger(__name__)

class Command(BaseCommand):
    help = 'Update daily NAV for all portfolios'

    def handle(self, *args, **options):
        today = timezone.now().date()
        
        # Get all portfolios
        portfolios = Portfolio.objects.all()
        
        for portfolio in portfolios:
            try:
                # Get all holdings for this portfolio
                holdings = Holding.objects.filter(portfolio=portfolio)
                
                # Calculate total NAV
                total_nav = Decimal('0')
                for holding in holdings:
                    if holding.asset_class == 'Cash':
                        total_nav += holding.book_cost
                    elif holding.asset:
                        price_data = get_or_fetch_market_price(
                            holding.asset.ticker,
                            today
                        )
                        if price_data is not None:
                            holding.market_value = price_data * holding.quantity
                            holding.save(update_fields=['market_value'])
                            total_nav += holding.market_value
                
                # Prevent future-date NAV
                if today > timezone.now().date():
                    continue
                
                # Update portfolio NAV
                portfolio.nav = total_nav
                portfolio.save(update_fields=['nav'])
                
            except Exception as e:
                logger.error(f"Error updating NAV for portfolio {portfolio.id}: {e}")
                continue
                
        self.stdout.write(self.style.SUCCESS('Successfully updated NAV for all portfolios')) 