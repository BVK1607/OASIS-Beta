from django.core.management.base import BaseCommand
from django.db import connection
from django.db.models import F, Q, Count, Sum
from portfolio.models import Portfolio, Holding, MarketPrice, HoldingReturn, PortfolioNAV
from django.utils import timezone
from datetime import timedelta
import logging
from decimal import Decimal

logger = logging.getLogger(__name__)

class Command(BaseCommand):
    help = 'Audit the NAV calculation process from market prices to holding returns to NAV'

    def handle(self, *args, **options):
        self.stdout.write('Starting NAV process audit...\n')
        today = timezone.now().date()
        week_ago = today - timedelta(days=7)

        # 1. Market Price Coverage
        self.stdout.write('\n1. Market Price Coverage Audit')
        holdings = Holding.objects.exclude(asset_class='Cash')
        for holding in holdings:
            # Check last week's price coverage
            prices = MarketPrice.objects.filter(
                ticker=holding.ticker,
                date__range=(week_ago, today)
            ).order_by('date')
            
            missing_dates = []
            current_date = week_ago
            while current_date <= today:
                if not prices.filter(date=current_date).exists():
                    missing_dates.append(current_date)
                current_date += timedelta(days=1)
            
            if missing_dates:
                self.stdout.write(f'- Holding {holding.ticker}: Missing prices for dates: {missing_dates}')
            
            # Check price staleness
            latest_price = prices.last()
            if latest_price:
                days_old = (today - latest_price.date).days
                if days_old > 3:
                    self.stdout.write(f'- WARNING: {holding.ticker} prices are {days_old} days old')

        # 2. Holding Returns Calculation Audit
        self.stdout.write('\n2. Holding Returns Audit')
        for holding in holdings:
            # Get a sample of holding returns and verify calculations
            returns = HoldingReturn.objects.filter(
                holding=holding,
                date__range=(week_ago, today)
            ).order_by('date')
            
            prev_market_value = None
            for ret in returns:
                # Verify market value matches holding's current values
                if ret.date == today:
                    if ret.market_value != holding.market_value:
                        self.stdout.write(
                            f'- Mismatch: {holding.ticker} market value mismatch on {ret.date}:\n'
                            f'  Holding: {holding.market_value}, Return record: {ret.market_value}'
                        )
                
                # Verify daily return calculation
                if prev_market_value and prev_market_value > 0:
                    expected_return = ((ret.market_value - prev_market_value) / prev_market_value) * 100
                    if abs(expected_return - ret.daily_return) > Decimal('0.01'):
                        self.stdout.write(
                            f'- Error: {holding.ticker} return calculation error on {ret.date}:\n'
                            f'  Expected: {expected_return}, Actual: {ret.daily_return}'
                        )
                
                prev_market_value = ret.market_value

        # 3. NAV Calculation Audit
        self.stdout.write('\n3. Portfolio NAV Audit')
        portfolios = Portfolio.objects.all()
        for portfolio in portfolios:
            navs = PortfolioNAV.objects.filter(
                portfolio=portfolio,
                date__range=(week_ago, today)
            ).order_by('date')
            
            for nav_record in navs:
                # Calculate expected NAV from holdings
                holdings_value = Decimal('0')
                holdings_on_date = portfolio.holdings.filter(purchase_date__lte=nav_record.date)
                
                for holding in holdings_on_date:
                    if holding.asset_class == 'Cash':
                        holdings_value += holding.book_cost
                    else:
                        market_price = MarketPrice.objects.filter(
                            ticker=holding.ticker,
                            date=nav_record.date
                        ).first()
                        
                        if market_price:
                            holdings_value += market_price.price * holding.quantity
                
                if abs(holdings_value - nav_record.total_value) > Decimal('0.01'):
                    self.stdout.write(
                        f'- NAV Mismatch for Portfolio {portfolio.name} on {nav_record.date}:\n'
                        f'  Calculated: {holdings_value}, Recorded: {nav_record.total_value}'
                    )

            # Check NAV continuity
            prev_nav = None
            for nav_record in navs:
                if prev_nav:
                    # Check for unusual jumps in NAV (>10% daily change)
                    if prev_nav.total_value > 0:
                        daily_change = ((nav_record.total_value - prev_nav.total_value) / prev_nav.total_value) * 100
                        if abs(daily_change) > 10:
                            self.stdout.write(
                                f'- Large NAV change for Portfolio {portfolio.name}:\n'
                                f'  {nav_record.date}: {daily_change}% change from previous day'
                            )
                prev_nav = nav_record

        # 4. Data Consistency Checks
        self.stdout.write('\n4. Data Consistency Checks')
        
        # Check for holdings without returns
        holdings_without_returns = Holding.objects.exclude(
            asset_class='Cash'
        ).exclude(
            id__in=HoldingReturn.objects.values('holding_id')
        )
        if holdings_without_returns.exists():
            self.stdout.write('- Holdings with no return records:')
            for h in holdings_without_returns:
                self.stdout.write(f'  * {h.ticker} (ID: {h.id})')

        # Check for portfolios without NAV
        portfolios_without_nav = Portfolio.objects.exclude(
            id__in=PortfolioNAV.objects.values('portfolio_id')
        )
        if portfolios_without_nav.exists():
            self.stdout.write('- Portfolios with no NAV records:')
            for p in portfolios_without_nav:
                self.stdout.write(f'  * {p.name} (ID: {p.id})')

        # Check for gaps in NAV dates
        for portfolio in portfolios:
            navs = PortfolioNAV.objects.filter(
                portfolio=portfolio,
                date__range=(week_ago, today)
            ).order_by('date')
            
            dates_set = set(navs.values_list('date', flat=True))
            missing_dates = []
            
            current_date = week_ago
            while current_date <= today:
                if current_date.weekday() < 5 and current_date not in dates_set:  # Skip weekends
                    missing_dates.append(current_date)
                current_date += timedelta(days=1)
            
            if missing_dates:
                self.stdout.write(f'- Portfolio {portfolio.name} missing NAV for dates: {missing_dates}')

        self.stdout.write('\nNAV process audit complete.')

    def audit_nav_calculation(self, portfolio, nav_record):
        """Audit NAV calculation for a specific record"""
        try:
            # Get holdings active on this date
            holdings_on_date = portfolio.holdings.filter(purchase_date__lte=nav_record.date)
            
            # Calculate expected total value
            expected_total = Decimal('0')
            for holding in holdings_on_date:
                if holding.asset_class == 'Cash':
                    expected_total += holding.book_cost
                else:
                    price = MarketPrice.objects.filter(
                        ticker=holding.ticker,
                        date=nav_record.date
                    ).first()
                    
                    if price is None:
                        logger.error(f"Could not get price for {holding.ticker} on {nav_record.date}")
                        continue
                    expected_total += price * holding.quantity
            
            # Compare with recorded value
            if abs(expected_total - nav_record.total_value) > Decimal('0.01'):
                logger.error(
                    f"NAV mismatch for portfolio {portfolio.id} on {nav_record.date}: "
                    f"expected={expected_total}, actual={nav_record.total_value}"
                )
                return False
            
            return True
            
        except Exception as e:
            logger.error(f"Error auditing NAV for portfolio {portfolio.id}: {e}")
            return False 