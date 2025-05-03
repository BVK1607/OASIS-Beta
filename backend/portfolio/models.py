from django.db import models, transaction
from django.core.validators import MinValueValidator
from decimal import Decimal, ROUND_HALF_UP
from django.utils import timezone
from datetime import date, datetime, timedelta
from django.core.exceptions import ValidationError
import logging
from .market_data import get_market_price, get_or_fetch_fx_rate, get_or_fetch_market_price
from django.db.models import Sum, F, Q, Min, Max
from typing import Optional, List, Dict, Any
from django.db.utils import OperationalError
import time

logger = logging.getLogger(__name__)

class Portfolio(models.Model):
    RISK_TOLERANCE_CHOICES = [
        ('Conservative', 'Conservative'),
        ('Moderate', 'Moderate'),
        ('Aggressive', 'Aggressive'),
    ]
    
    CURRENCY_CHOICES = [
        ('USD', 'US Dollar'),
        ('GBP', 'British Pound'),
    ]
    
    name = models.CharField(max_length=100)
    description = models.TextField(blank=True, default='')
    risk_tolerance = models.CharField(max_length=20, choices=RISK_TOLERANCE_CHOICES, default='Moderate')
    target_return = models.DecimalField(max_digits=5, decimal_places=2, blank=True, null=True)
    creation_date = models.DateField(default=timezone.now)
    created_at = models.DateTimeField(auto_now_add=True)
    updated_at = models.DateTimeField(auto_now=True)
    base_currency = models.CharField(max_length=3, choices=CURRENCY_CHOICES, default='USD')

    class Meta:
        db_table = 'portfolio'

    def __str__(self):
        return f"{self.name} ({self.risk_tolerance})"
    
    def update_market_prices(self):
        """Queue market price updates for all holdings"""
        from .task_queue import task_queue
        task_queue.add_live_prices_task(self.id)
    
    @property
    def total_market_value(self):
        """Calculate total market value of all holdings"""
        return sum(holding.market_value or Decimal('0') for holding in self.holdings.all())
    
    @property
    def total_book_cost(self):
        """Calculate total book cost of all holdings"""
        return sum(holding.book_cost or Decimal('0') for holding in self.holdings.all())
    
    @property
    def total_gain_loss(self):
        """Calculate total unrealized gain/loss"""
        return self.total_market_value - self.total_book_cost

    def recalculate_nav_from_date(self, from_date: date) -> bool:
        """
        Recalculate NAV history from the specified date.
        Returns True if successful, False otherwise.
        """
        try:
            # Get all dates from from_date to today
            dates = pd.date_range(start=from_date, end=date.today(), freq='D')
            
            for d in dates:
                nav = self.get_nav_in_currency(self.base_currency, d.date())
                if nav is not None:
                    NAVHistory.objects.update_or_create(
                        portfolio=self,
                        date=d.date(),
                        defaults={'nav': nav}
                    )

            return True

        except Exception as e:
            logger.error(f"Error recalculating NAV for portfolio {self.id}: {e}")
            return False

    def calculate_nav(self, date=None):
        """
        Calculate NAV for a specific date with staleness check.
        NAV is indexed to 100 at portfolio inception.
        """
        if date is None:
            date = timezone.now().date()
        
        # Check for stale prices
        stale_prices = MarketPrice.objects.filter(
            ticker__in=self.holdings.values_list('ticker', flat=True),
            date__lt=date,
            last_updated__lt=timezone.now() - timedelta(days=1)
        ).exists()
        
        if stale_prices:
            logger.warning(f"Stale prices detected for portfolio {self.id} on {date}")
        
        # Get the initial portfolio value (at inception)
        inception_date = self.creation_date
        inception_value = None
        if not hasattr(self, '_inception_value'):
            inception_nav = PortfolioNAV.objects.filter(
                portfolio=self,
                date=inception_date
            ).first()
            
            if inception_nav:
                inception_value = inception_nav.total_value
            else:
                # Calculate inception value if not stored
                inception_value = Decimal('0')
                for holding in self.holdings.filter(trade_date=inception_date):
                    price = holding.get_price(inception_date)
                    if price:
                        inception_value += holding.quantity * price
            
            self._inception_value = inception_value if inception_value > 0 else Decimal('1')
        
        # Calculate current total value
        total_value = Decimal('0')
        for holding in self.holdings.filter(trade_date__lte=date):
            price = holding.get_price(date)
            if price:
                total_value += holding.quantity * price
        
        # Index to 100
        if self._inception_value and self._inception_value > 0:
            return Decimal('100.0') * (total_value / self._inception_value)
        
        return total_value  # Return absolute value if no inception value available

    def validate_portfolio_data(self, date: date) -> bool:
        """
        Validate portfolio data for a given date
        Returns True if data is valid, False otherwise
        """
        try:
            # Check holdings
            holdings = self.holdings.filter(date=date)
            if not holdings.exists():
                logger.warning(f"No holdings found for portfolio {self.name} on {date}")
                return False
            
            # Check market prices
            for holding in holdings:
                if not MarketPrice.objects.filter(
                    ticker=holding.ticker,
                    date=date
                ).exists():
                    logger.error(f"No market price found for {holding.ticker} on {date}")
                    return False
                
            # Check NAV calculation
            calculated_nav = self.calculate_nav(date)
            if calculated_nav is None:
                logger.error(f"Failed to calculate NAV for portfolio {self.name} on {date}")
                return False
            
            # Check for significant NAV changes
            prev_nav = self.calculate_nav(date - timedelta(days=1))
            if prev_nav and abs(calculated_nav - prev_nav) / prev_nav > 0.1:  # 10% threshold
                logger.warning(f"Significant NAV change detected for portfolio {self.name} on {date}")
                return False
            
            return True
        
        except Exception as e:
            logger.error(f"Error validating portfolio data for {self.name} on {date}: {str(e)}")
            return False

    def check_data_consistency(self, date: date) -> Dict[str, List[str]]:
        """
        Check data consistency across the system for a given date
        Returns a dictionary of issues found
        """
        issues = {
            'missing_prices': [],
            'missing_holdings': [],
            'invalid_nav': [],
            'data_gaps': []
        }
        
        try:
            # Check holdings
            holdings = self.holdings.filter(date=date)
            if not holdings.exists():
                issues['missing_holdings'].append(f"Portfolio {self.name}")
                return issues
                
            # Check market prices
            for holding in holdings:
                if not MarketPrice.objects.filter(
                    ticker=holding.ticker,
                    date=date
                ).exists():
                    issues['missing_prices'].append(f"{holding.ticker} for {self.name}")
                    
            # Check NAV
            nav = self.calculate_nav(date)
            if nav is None:
                issues['invalid_nav'].append(f"Portfolio {self.name}")
                
            # Check for data gaps
            prev_date = date - timedelta(days=1)
            if not self.holdings.filter(date=prev_date).exists():
                issues['data_gaps'].append(f"Portfolio {self.name}")
                
            return issues
        
        except Exception as e:
            logger.error(f"Error checking data consistency for {date}: {str(e)}")
            return issues

    def check_portfolio_changes(self, date: date, threshold: float = 0.1) -> Dict[str, List[str]]:
        """
        Check for significant changes in portfolio values
        threshold: percentage change that triggers an alert (default 10%)
        Returns a dictionary of significant changes found
        """
        changes = {
            'large_nav_changes': [],
            'large_position_changes': []
        }
        
        try:
            # Get all portfolios
            portfolios = Portfolio.objects.all()
            prev_date = date - timedelta(days=1)
            
            for portfolio in portfolios:
                # Check NAV changes
                current_nav = portfolio.calculate_nav(date)
                prev_nav = portfolio.calculate_nav(prev_date)
                
                if current_nav and prev_nav:
                    nav_change = abs((current_nav - prev_nav) / prev_nav)
                    if nav_change > threshold:
                        changes['large_nav_changes'].append(
                            f"Portfolio {portfolio.name}: {nav_change:.2%} change"
                        )
                
                # Check individual position changes
                current_holdings = portfolio.holdings.filter(date=date)
                prev_holdings = portfolio.holdings.filter(date=prev_date)
                
                for holding in current_holdings:
                    prev_holding = prev_holdings.filter(ticker=holding.ticker).first()
                    if prev_holding:
                        position_change = abs((holding.quantity - prev_holding.quantity) / prev_holding.quantity)
                        if position_change > threshold:
                            changes['large_position_changes'].append(
                                f"{holding.ticker} in {portfolio.name}: {position_change:.2%} change"
                            )
            
            return changes
        
        except Exception as e:
            logger.error(f"Error checking portfolio changes for {date}: {str(e)}")
            return changes

    def get_nav_in_currency(self, target_currency: str, date: date = None) -> Optional[Decimal]:
        """
        Get the NAV of the portfolio in the specified currency.
        If date is not provided, uses the current date.
        """
        try:
            if date is None:
                date = timezone.now().date()
            
            logger.info(f"Calculating NAV in {target_currency} for portfolio {self.id} on {date}")
            
            total_value = Decimal('0')
            for holding in self.holdings.all():
                logger.info(f"Processing holding {holding.ticker} ({holding.asset_class})")
                
                if holding.asset_class == 'Cash':
                    value = holding.book_cost
                    if holding.currency != target_currency:
                        rate = get_or_fetch_fx_rate(holding.currency, target_currency, date)
                        logger.info(f"Got FX rate for {holding.currency}/{target_currency}: {rate}")
                        if rate is None:
                            logger.error(f"Could not get FX rate for {holding.currency}/{target_currency}")
                            return None
                        value = value * rate
                    total_value += value
                    logger.info(f"Added cash value: {value} {target_currency}")
                else:
                    value = holding.get_value_in_currency(target_currency, date)
                    if value is None:
                        logger.error(f"Could not get value for {holding.ticker} in {target_currency}")
                        return None
                    total_value += value
                    logger.info(f"Added security value: {value} {target_currency}")

            logger.info(f"Total portfolio value: {total_value} {target_currency}")
            return total_value

        except Exception as e:
            logger.error(f"Error getting NAV in {target_currency} for portfolio {self.id}: {e}")
            return None

class Holding(models.Model):
    ASSET_CLASS_CHOICES = [
        ('Equity', 'Equity'),
        ('Fixed Income', 'Fixed Income'),
        ('Alternatives', 'Alternatives'),
        ('Cash', 'Cash'),
    ]

    portfolio = models.ForeignKey(Portfolio, on_delete=models.CASCADE, related_name='holdings')
    asset_class = models.CharField(max_length=20, choices=ASSET_CLASS_CHOICES, default='Equity')
    isin = models.CharField(max_length=12, blank=True, null=True)
    ticker = models.CharField(max_length=20, null=True, blank=True)
    quantity = models.DecimalField(max_digits=20, decimal_places=6, default=Decimal('0'))
    book_cost = models.DecimalField(
        max_digits=15,
        decimal_places=2,
        validators=[MinValueValidator(Decimal('0.01'))],
        default=Decimal('0'),
    )
    currency = models.CharField(max_length=3, default='USD')
    purchase_date = models.DateField(default=date.today)
    purchase_price = models.DecimalField(max_digits=20, decimal_places=6, default=Decimal('0'))
    current_price = models.DecimalField(max_digits=20, decimal_places=6, null=True, blank=True)
    market_value = models.DecimalField(
        max_digits=15,
        decimal_places=2,
        null=True,
        blank=True
    )
    last_updated = models.DateTimeField(auto_now=True)

    created_at = models.DateTimeField(auto_now_add=True)
    updated_at = models.DateTimeField(auto_now=True)

    name = models.CharField(max_length=128, blank=True, null=True)

    class Meta:
        db_table = 'holding'
        constraints = [
            models.UniqueConstraint(
                fields=['portfolio', 'isin', 'purchase_date'],
                condition=~models.Q(asset_class='Cash'),
                name='unique_portfolio_isin_date_non_cash'
            ),
            models.CheckConstraint(
                check=models.Q(quantity__gte=0),
                name='positive_quantity'
            ),
            models.CheckConstraint(
                check=models.Q(book_cost__gte=0),
                name='positive_book_cost'
            ),
            models.CheckConstraint(
                check=models.Q(market_value__gte=0),
                name='positive_market_value'
            )
        ]
        indexes = [
            models.Index(fields=['portfolio', 'ticker']),
            models.Index(fields=['ticker']),
            models.Index(fields=['last_updated']),
            models.Index(fields=['portfolio', '-purchase_date']),
        ]
        ordering = ['-created_at']

    def __str__(self):
        return f"{self.ticker or 'Unknown'} ({self.quantity} @ {self.purchase_price} {self.currency})"

    def clean(self):
        """Validate the model"""
        if self.asset_class == 'Cash':
            if self.isin or self.ticker or self.quantity:
                raise ValidationError('Cash positions should not have ISIN, ticker, or quantity')
        else:
            if not self.isin:
                raise ValidationError('ISIN is required for non-cash holdings')
            if not self.ticker:
                raise ValidationError('Ticker is required for non-cash holdings')
            if not self.quantity:
                raise ValidationError('Quantity is required for non-cash holdings')

    @transaction.atomic
    def save(self, *args, **kwargs):
        """Save the holding with proper decimal handling"""
        # Ensure consistent decimal precision
        if self.quantity is not None:
            self.quantity = Decimal(str(self.quantity)).quantize(
                Decimal('0.0001'),
                rounding=ROUND_HALF_UP
            )
        if self.book_cost is not None:
            self.book_cost = Decimal(str(self.book_cost)).quantize(
                Decimal('0.01'),
                rounding=ROUND_HALF_UP
            )
        if self.market_value is not None:
            self.market_value = Decimal(str(self.market_value)).quantize(
                Decimal('0.01'),
                rounding=ROUND_HALF_UP
            )
        if self.current_price is not None:
            self.current_price = Decimal(str(self.current_price)).quantize(
                Decimal('0.01'),
                rounding=ROUND_HALF_UP
            )

        # For cash positions, set market value equal to book cost
        if self.asset_class == 'Cash':
            self.current_price = self.book_cost
            self.market_value = self.book_cost
            self.last_updated = timezone.now()

        self.clean()
        is_new = self.pk is None
        old_holding = None if is_new else Holding.objects.select_for_update().get(pk=self.pk)
        
        # Save the holding first
        super().save(*args, **kwargs)
        
        # Determine the earliest date to recalculate from
        earliest_date = self.purchase_date
        
        # If this is an update, check if purchase_date changed
        if not is_new and old_holding and old_holding.purchase_date != self.purchase_date:
            earliest_date = min(earliest_date, old_holding.purchase_date)
        
        # Recalculate NAV from the earliest affected date
        self.portfolio.recalculate_nav_from_date(earliest_date)

    @transaction.atomic
    def update_market_price(self) -> bool:
        """
        Update the current market price using yfinance.
        Returns True if successful, False otherwise.
        """
        try:
            price = get_or_fetch_market_price(self.ticker, date.today())
            if price is None:
                return False

            self.current_price = price
            self.market_value = price * self.quantity
            self.last_updated = timezone.now()
            self.save()
            return True

        except Exception as e:
            logger.error(f"Error updating market price for {self.ticker}: {e}")
            return False

    def get_value_in_currency(self, target_currency: str, date: date = None) -> Optional[Decimal]:
        """
        Get the value of the holding in the specified currency.
        If date is not provided, uses the current price.
        """
        try:
            logger.info(f"Getting value for {self.ticker} in {target_currency} on {date}")
            
            # For cash positions, just convert the book cost
            if self.asset_class == 'Cash':
                if self.currency == target_currency:
                    logger.info(f"Cash position in same currency: {self.book_cost} {self.currency}")
                    return self.book_cost
                rate = get_or_fetch_fx_rate(self.currency, target_currency, date or timezone.now().date())
                logger.info(f"Got FX rate for cash: {self.currency}/{target_currency} = {rate}")
                if rate is None:
                    logger.error(f"Could not get FX rate for cash: {self.currency}/{target_currency}")
                    return None
                value = self.book_cost * rate
                logger.info(f"Converted cash value: {self.book_cost} {self.currency} = {value} {target_currency}")
                return value

            # For securities, get the price and convert
            if date is None:
                price = self.current_price
                logger.info(f"Current price for {self.ticker}: {price}")
                if price is None:
                    # Try to fetch current price
                    price_data = get_market_price(self.ticker)
                    logger.info(f"Fetched current price data: {price_data}")
                    if price_data and price_data.get('price'):
                        price = price_data['price']
                    else:
                        logger.error(f"Could not get current price for {self.ticker}")
                        return None
            else:
                # Try to get historical price
                market_price = MarketPrice.objects.filter(
                    ticker=self.ticker,
                    date=date
                ).first()
                if market_price:
                    price = market_price.price
                    logger.info(f"Got historical price from DB: {price}")
                else:
                    # Try to fetch historical price
                    price_data = get_or_fetch_market_price(self.ticker, date)
                    logger.info(f"Fetched historical price data: {price_data}")
                    if price_data and price_data.get('price'):
                        price = price_data['price']
                    else:
                        logger.error(f"Could not get historical price for {self.ticker} on {date}")
                        return None

            value = self.quantity * price
            logger.info(f"Base value: {value} {self.currency}")
            
            if self.currency != target_currency:
                rate = get_or_fetch_fx_rate(self.currency, target_currency, date or timezone.now().date())
                logger.info(f"Got FX rate: {self.currency}/{target_currency} = {rate}")
                if rate is None:
                    logger.error(f"Could not get FX rate: {self.currency}/{target_currency}")
                    return None
                value = value * rate
                logger.info(f"Converted value: {value} {target_currency}")
            return value

        except Exception as e:
            logger.error(f"Error getting value in {target_currency} for {self.ticker}: {e}")
            return None

    def get_price(self, date) -> Optional[Decimal]:
        """
        Get the price for this holding on a specific date.
        Returns None if no price is available.
        """
        if self.asset_class == 'Cash':
            return self.book_cost

        try:
            # Try to get from MarketPrice first
            market_price = MarketPrice.objects.filter(
                ticker=self.ticker,
                date=date
            ).first()

            if market_price:
                return market_price.price

            # If not found, try to fetch from market data
            from .market_data import get_or_fetch_market_price
            price_data = get_or_fetch_market_price(self.ticker, date)
            if price_data and price_data.get('price'):
                return price_data['price']

            return None

        except Exception as e:
            logger.error(f"Error getting price for {self.ticker} on {date}: {str(e)}")
            return None

class PortfolioNAV(models.Model):
    """
    Stores daily portfolio NAV and total value.
    total_value: The actual sum of all holdings' market values
    nav: The indexed NAV value, rebased to 100 at portfolio start
    """
    portfolio = models.ForeignKey(Portfolio, on_delete=models.CASCADE, related_name='navs')
    date = models.DateField()
    total_value = models.DecimalField(max_digits=20, decimal_places=2)
    nav = models.DecimalField(max_digits=20, decimal_places=6)
    daily_return = models.DecimalField(max_digits=10, decimal_places=6, null=True)
    last_updated = models.DateTimeField(auto_now=True)

    class Meta:
        db_table = 'portfolio_nav'
        indexes = [
            models.Index(fields=['portfolio', 'date'], name='portfolio_nav_port_date_idx'),
            models.Index(fields=['date'], name='idx_portfolio_nav_date'),
        ]
        unique_together = ('portfolio', 'date')
        ordering = ['date']

    def __str__(self):
        return f"{self.portfolio.name} NAV on {self.date}"

class HoldingReturn(models.Model):
    """
    Daily returns for each holding in a portfolio
    """
    portfolio = models.ForeignKey(Portfolio, on_delete=models.CASCADE, related_name='holding_returns')
    holding = models.ForeignKey(Holding, on_delete=models.CASCADE, related_name='returns', null=True, blank=True)
    date = models.DateField()
    daily_return = models.DecimalField(max_digits=10, decimal_places=6, null=True)
    market_value = models.DecimalField(max_digits=15, decimal_places=2, null=True)

    class Meta:
        db_table = 'holding_returns'
        indexes = [
            models.Index(fields=['portfolio', 'date'], name='holding_returns_port_date_idx'),
            models.Index(fields=['holding', 'date'], name='holding_returns_hold_date_idx'),
        ]
        unique_together = [['portfolio', 'holding', 'date']]

    def __str__(self):
        return f"{self.holding.ticker if self.holding else 'Unknown'} return on {self.date}: {self.daily_return}%"

    @classmethod
    def calculate_return(cls, current_value: Decimal, previous_value: Decimal) -> Optional[Decimal]:
        """Calculate daily return with proper handling of edge cases"""
        if not previous_value or previous_value <= 0:
            return None
        return ((current_value / previous_value) - Decimal('1')) * Decimal('100')

    @classmethod
    def calculate_and_save(cls, portfolio, holding, date):
        """
        Calculate and save holding return using stored market prices
        """
        try:
            # Get market price for the current date
            current_price = MarketPrice.objects.filter(
                ticker=holding.ticker,
                date=date
            ).first()

            if not current_price:
                logger.warning(f"No market price found for {holding.ticker} on {date}")
                return None

            # Calculate market value
            market_value = current_price.price * holding.quantity

            # Get previous return for comparison
            prev_return = cls.objects.filter(
                holding=holding,
                date__lt=date
            ).order_by('-date').first()

            daily_return = None
            if prev_return and prev_return.market_value:
                daily_return = cls.calculate_return(market_value, prev_return.market_value)

            # Create or update return record
            return cls.objects.update_or_create(
                portfolio=portfolio,
                holding=holding,
                date=date,
                defaults={
                    'market_value': market_value,
                    'daily_return': daily_return
                }
            )[0]

        except Exception as e:
            logger.error(f"Error calculating return for {holding.ticker} on {date}: {str(e)}")
            return None

    @classmethod
    def bulk_calculate_returns(cls, portfolio, date):
        """
        Efficiently calculate returns for multiple holdings using stored market prices
        """
        try:
            # Get all active holdings for the portfolio
            holdings = portfolio.holdings.filter(purchase_date__lte=date)

            # Get market prices for all holdings on this date
            market_prices = {
                mp.ticker: mp.price
                for mp in MarketPrice.objects.filter(
                    ticker__in=holdings.values_list('ticker', flat=True),
                    date=date
                )
            }

            # Get previous returns for all holdings
            prev_returns = {
                hr.holding_id: hr
                for hr in cls.objects.filter(
                    portfolio=portfolio,
                    holding__in=holdings,
                    date__lt=date
                ).order_by('holding_id', '-date')
            }

            # Prepare bulk creation data
            returns_to_create = []
            for holding in holdings:
                if holding.ticker not in market_prices:
                    continue

                market_value = market_prices[holding.ticker] * holding.quantity
                prev_return = prev_returns.get(holding.id)
                daily_return = None

                if prev_return and prev_return.market_value:
                    daily_return = cls.calculate_return(market_value, prev_return.market_value)

                returns_to_create.append(cls(
                    portfolio=portfolio,
                    holding=holding,
                    date=date,
                    market_value=market_value,
                    daily_return=daily_return
                ))

            # Bulk create with ignore_conflicts to handle race conditions
            if returns_to_create:
                cls.objects.bulk_create(
                    returns_to_create,
                    ignore_conflicts=True,
                    batch_size=100
                )

        except Exception as e:
            logger.error(f"Error in bulk return calculation for portfolio {portfolio.id} on {date}: {str(e)}")

class BenchmarkReturn(models.Model):
    benchmark_name = models.CharField(max_length=50)
    display_name = models.CharField(max_length=100, blank=True, null=True)
    date = models.DateField()
    close_price = models.DecimalField(max_digits=20, decimal_places=6, null=True)
    daily_return = models.DecimalField(max_digits=10, decimal_places=6)
    return_index = models.DecimalField(max_digits=20, decimal_places=6, null=True)

    class Meta:
        db_table = 'benchmark_return'
        unique_together = ('benchmark_name', 'date')
        indexes = [
            models.Index(fields=['benchmark_name', 'date'], name='bench_name_date_idx'),
        ]
        ordering = ['benchmark_name', 'date']

    def __str__(self):
        return f"{self.display_name or self.benchmark_name} return on {self.date}"

class RebalancePlan(models.Model):
    portfolio = models.ForeignKey(Portfolio, on_delete=models.CASCADE, related_name='rebalance_snapshots')
    date_created = models.DateTimeField(auto_now_add=True)
    target_weights = models.JSONField()
    current_weights = models.JSONField()
    action_plan = models.JSONField()
    comment = models.TextField(blank=True, null=True)
    executed = models.BooleanField(default=False)

    class Meta:
        db_table = 'rebalance_plan'

    def __str__(self):
        return f"RebalancePlan for {self.portfolio.name} at {self.date_created}"

# --- TradeHistory model ---
class TradeHistory(models.Model):
    TRADE_TYPE_CHOICES = [
        ("Buy", "Buy"),
        ("Sell", "Sell"),
        ("Add", "Add"),
        ("Adjust", "Adjust"),
        ("Dividend", "Dividend"),
    ]
    portfolio = models.ForeignKey(Portfolio, on_delete=models.CASCADE, related_name='trade_history')
    date = models.DateTimeField(auto_now_add=True)
    trade_type = models.CharField(max_length=16, choices=TRADE_TYPE_CHOICES)
    ticker = models.CharField(max_length=10)
    isin = models.CharField(max_length=12, blank=True, null=True)
    asset_class = models.CharField(max_length=20)
    quantity = models.DecimalField(max_digits=15, decimal_places=4)
    price_at_trade = models.DecimalField(max_digits=15, decimal_places=4)
    book_cost = models.DecimalField(max_digits=15, decimal_places=2)
    gross_amount = models.DecimalField(max_digits=15, decimal_places=2)
    cash_flow_impact = models.DecimalField(max_digits=15, decimal_places=2)
    currency = models.CharField(max_length=3)
    portfolio_weight_change = models.DecimalField(max_digits=7, decimal_places=4, blank=True, null=True)
    notes = models.TextField(blank=True, null=True)

    class Meta:
        db_table = 'trade_history'

    def save(self, *args, **kwargs):
        # Calculate cash_flow_impact: negative for buys, positive for sells
        if self.trade_type == "Buy":
            self.cash_flow_impact = -abs(self.quantity * self.price_at_trade)
        elif self.trade_type == "Sell":
            self.cash_flow_impact = abs(self.quantity * self.price_at_trade)
        else:
            self.cash_flow_impact = self.quantity * self.price_at_trade
        self.gross_amount = self.book_cost  # For now, same as book_cost
        super().save(*args, **kwargs)

    def __str__(self):
        return f"{self.trade_type} {self.ticker} ({self.quantity}) on {self.date.strftime('%Y-%m-%d %H:%M')} for {self.portfolio.name}"

class MarketPrice(models.Model):
    """
    Stores historical market prices for tickers.
    Each price is associated with a portfolio.
    """
    portfolio = models.ForeignKey(
        Portfolio,
        on_delete=models.CASCADE,
        related_name='market_prices'
    )
    ticker = models.CharField(max_length=20)
    date = models.DateField()
    price = models.DecimalField(max_digits=20, decimal_places=4)
    last_updated = models.DateTimeField(auto_now=True)

    class Meta:
        db_table = 'portfolio_marketprice'
        unique_together = ('portfolio', 'ticker', 'date')
        indexes = [
            models.Index(fields=['ticker'], name='mp_ticker_idx'),
            models.Index(fields=['date'], name='mp_date_idx'),
            models.Index(fields=['portfolio', 'ticker'], name='mp_port_ticker_idx'),
            models.Index(fields=['portfolio', 'ticker', 'date'], name='mp_port_ticker_date_idx'),
        ]

    def __str__(self):
        return f"{self.ticker} - {self.date}: {self.price} (Portfolio: {self.portfolio.name})"

    def save(self, *args, **kwargs):
        super().save(*args, **kwargs)

class FXRate(models.Model):
    """Model to store currency exchange rates"""
    base_currency = models.CharField(max_length=3)
    target_currency = models.CharField(max_length=3)
    rate = models.DecimalField(max_digits=10, decimal_places=6)
    date = models.DateField()
    last_updated = models.DateTimeField(auto_now=True)

    class Meta:
        db_table = 'fx_rate'
        unique_together = ('base_currency', 'target_currency', 'date')
        indexes = [
            models.Index(fields=['base_currency', 'target_currency', 'date']),
        ]

    def __str__(self):
        return f"{self.base_currency}/{self.target_currency} = {self.rate} on {self.date}"
