from django.core.management.base import BaseCommand
from django.utils import timezone
from portfolio.models import Portfolio, Holding, PortfolioNAV, HoldingReturn
from portfolio.market_data import get_market_price, PriceValidationError
from decimal import Decimal
import logging
from datetime import datetime, timedelta
import pytz
from django.db import transaction

logger = logging.getLogger(__name__)

class Command(BaseCommand):
    help = 'Update NAV and returns for all portfolios for the current day'

    def handle(self, *args, **options):
        uk_tz = pytz.timezone('Europe/London')
        current_time = timezone.now().astimezone(uk_tz)
        
        # Run between 8:00 and 23:00 UK time
        if current_time.hour < 8 or current_time.hour >= 23:
            logger.info(f"Skipping NAV update - current time {current_time.strftime('%H:%M')} is outside update window")
            return
            
        today = current_time.date()
        yesterday = today - timedelta(days=1)

        logger.info(f"Starting daily NAV update at {current_time}")
        
        validation_issues = []

        for portfolio in Portfolio.objects.all():
            try:
                self.stdout.write(f"Processing portfolio: {portfolio.name}")
                
                # Calculate total market value
                total_value = Decimal('0')
                portfolio_warnings = []
                
                # Get all active holdings
                active_holdings = list(portfolio.holdings.all())
                
                # First calculate total value including both cash and securities
                for holding in active_holdings:
                    if holding.asset_class == 'Cash':
                        total_value += holding.book_cost
                        continue

                    if not holding.ticker:
                        continue

                    try:
                        # Get today's price with validation
                        market_data = get_market_price(holding.ticker)
                        
                        if not market_data['price']:
                            logger.warning(f"No price data available for {holding.ticker}")
                            continue
                            
                        # Update holding's current price and market value
                        holding.current_price = market_data['price']
                        holding.market_value = market_data['price'] * holding.quantity
                        holding.last_price_update = market_data['timestamp']
                        holding.save()

                        total_value += holding.market_value or Decimal('0')
                        
                    except PriceValidationError as e:
                        error_msg = f"Error processing holding {holding.ticker}: {str(e)}"
                        logger.error(error_msg)
                        portfolio_warnings.append(error_msg)
                        continue

                # Log any validation issues for this portfolio
                if portfolio_warnings:
                    validation_issues.append({
                        'portfolio': portfolio.name,
                        'warnings': portfolio_warnings
                    })

                # Get previous NAV for daily return calculation
                prev_nav = PortfolioNAV.objects.filter(
                    portfolio=portfolio,
                    date__lt=today
                ).order_by('-date').first()

                daily_return = None
                if prev_nav and prev_nav.total_value > 0:
                    daily_return = ((total_value / prev_nav.total_value) - 1) * 100
                
                # Save or update today's NAV
                with transaction.atomic():
                    PortfolioNAV.objects.update_or_create(
                        portfolio=portfolio,
                        date=today,
                        defaults={
                            'nav': total_value.quantize(Decimal('0.0001')),  # NAV is just the total value
                            'total_value': total_value.quantize(Decimal('0.0001')),
                            'daily_return': daily_return.quantize(Decimal('0.000001')) if daily_return is not None else None
                        }
                    )

            except Exception as e:
                logger.error(f"Error processing portfolio {portfolio.name}: {e}")
                continue

        # Log any validation issues
        if validation_issues:
            logger.warning(f"Validation issues found: {validation_issues}")
            
        logger.info("Daily NAV update completed") 