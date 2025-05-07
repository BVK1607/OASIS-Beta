from django.db import models, transaction
from django.core.validators import MinValueValidator
from decimal import Decimal, ROUND_HALF_UP
from django.utils import timezone
from datetime import date, datetime, timedelta
from django.core.exceptions import ValidationError
import logging
from django.db.models import Sum, F, Q, Min, Max
from typing import Optional, List, Dict, Any
from django.db.utils import OperationalError
import time
import pandas as pd

logger = logging.getLogger(__name__)

class Portfolio(models.Model):
    RISK_TOLERANCE_CHOICES = [
        ('Low', 'Low'),
        ('Medium', 'Medium'),
        ('High', 'High')
    ]
    
    CURRENCY_CHOICES = [
        ('USD', 'US Dollar'),
        ('GBP', 'British Pound'),
        ('EUR', 'Euro')
    ]
    
    name = models.CharField(max_length=100)
    description = models.TextField(blank=True)
    risk_tolerance = models.CharField(
        max_length=10,
        choices=RISK_TOLERANCE_CHOICES,
        default='Medium'
    )
    target_return = models.DecimalField(
        max_digits=5,
        decimal_places=2,
        null=True,
        blank=True
    )
    creation_date = models.DateField(auto_now_add=True)
    base_currency = models.CharField(
        max_length=3,
        choices=CURRENCY_CHOICES,
        default='USD'
    )
    total_value = models.DecimalField(
        max_digits=20,
        decimal_places=2,
        default=Decimal('0.00')
    )
    
    def __str__(self):
        return self.name
    
    def validate_portfolio_data(self, target_date: date) -> bool:
        """
        Validate portfolio data for a given date
        Returns True if data is valid, False otherwise
        """
        try:
            # Ensure we have a date object
            if isinstance(target_date, datetime):
                target_date = target_date.date()
            
            # Check holdings
            holdings = self.holdings.filter(purchase_date__lte=target_date)
            if not holdings.exists():
                logger.warning(f"No holdings found for portfolio {self.name} on {target_date}")
                return False
            
            # Check market prices
            for holding in holdings:
                if holding.asset_class == 'Cash':
                    continue
                    
                if not GlobalMarketPrice.objects.filter(
                    asset=holding.asset,
                    date=target_date
                ).exists():
                    logger.error(f"No market price found for {holding.ticker} on {target_date}")
                    return False
                
            # Check NAV calculation
            nav = self.get_nav_in_currency(self.base_currency, target_date)
            if nav is None:
                logger.error(f"Failed to calculate NAV for portfolio {self.name} on {target_date}")
                return False
            
            # Check for significant NAV changes
            prev_nav = self.get_nav_in_currency(self.base_currency, target_date - timedelta(days=1))
            if prev_nav and prev_nav > 0:
                change = abs(nav - prev_nav) / prev_nav
                if change > Decimal('0.1'):  # 10% threshold
                    logger.warning(f"Significant NAV change detected for portfolio {self.name} on {target_date}")
                    return False
            
            return True
        
        except Exception as e:
            logger.error(f"Error validating portfolio data for {self.name} on {target_date}: {str(e)}")
            return False
    
    def get_nav_in_currency(self, target_currency: str, target_date: date = None) -> Optional[Decimal]:
        """
        Get the NAV of the portfolio in the specified currency.
        If date is not provided, uses the current date.
        """
        try:
            from .market_data import get_or_fetch_market_price
            if target_date is None:
                target_date = timezone.now().date()
            elif isinstance(target_date, datetime):
                target_date = target_date.date()
            
            logger.info(f"Calculating NAV in {target_currency} for portfolio {self.id} on {target_date}")
            
            total_value = Decimal('0')
            for holding in self.holdings.filter(purchase_date__lte=target_date):
                value = holding.get_value_in_currency(target_currency, target_date)
                if value is None:
                    logger.error(f"Could not get value for {holding.ticker} in {target_currency}")
                    return None
                total_value += value
            
            logger.info(f"Total portfolio value: {total_value} {target_currency}")
            return total_value
            
        except Exception as e:
            logger.error(f"Error getting NAV in {target_currency} for portfolio {self.id}: {e}")
            return None
    
    def recalculate_nav_from_date(self, from_date: date) -> bool:
        """
        Recalculate NAV history from the specified date.
        Returns True if successful, False otherwise.
        """
        try:
            from .market_data import get_or_fetch_market_price
            if isinstance(from_date, datetime):
                from_date = from_date.date()
                
            # Get all dates from from_date to today
            dates = pd.date_range(start=from_date, end=date.today(), freq='D')
            
            for d in dates:
                nav = self.get_nav_in_currency(self.base_currency, d.date())
                if nav is not None:
                    PortfolioNAV.objects.update_or_create(
                        portfolio=self,
                        date=d.date(),
                        defaults={
                            'total_value': nav,
                            'nav': nav,  # For now, just use the total value as NAV
                            'last_updated': timezone.now()
                        }
                    )
            
            return True
            
        except Exception as e:
            logger.error(f"Error recalculating NAV for portfolio {self.id}: {e}")
            return False

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

    def calculate_nav(self, date=None):
        """
        Calculate NAV for a specific date with staleness check.
        NAV is indexed to 100 at portfolio inception.
        """
        from .market_data import get_or_fetch_market_price
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
                for holding in self.holdings.filter(purchase_date=inception_date):
                    price = holding.get_price(inception_date)
                    if price:
                        inception_value += holding.quantity * price
            
            self._inception_value = inception_value if inception_value > 0 else Decimal('1')
        
        # Calculate current total value
        total_value = Decimal('0')
        for holding in self.holdings.filter(purchase_date__lte=date):
            price = holding.get_price(date)
            if price:
                total_value += holding.quantity * price
        
        # Index to 100
        if self._inception_value and self._inception_value > 0:
            return Decimal('100.0') * (total_value / self._inception_value)
        
        return total_value  # Return absolute value if no inception value available

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

    def delete(self, *args, **kwargs):
        # Explicitly delete all related objects in the correct order
        self.holding_returns.all().delete()
        self.nav_records.all().delete()
        self.trade_history.all().delete()
        self.market_prices.all().delete()
        self.holdings.all().delete()
        # If you have other related models, add them here
        super().delete(*args, **kwargs)

class Asset(models.Model):
    MARKET_CHOICES = [
        ('US', 'US Market'),
        ('UK', 'UK Market'),
        ('EU', 'European Market'),
    ]
    
    ticker = models.CharField(max_length=20, unique=True)
    isin = models.CharField(max_length=12, unique=True, null=True, blank=True)
    name = models.CharField(max_length=100)
    asset_class = models.CharField(max_length=20)
    currency = models.CharField(max_length=3, default='USD')
    market = models.CharField(max_length=2, choices=MARKET_CHOICES, default='US')
    last_updated = models.DateTimeField(auto_now=True)

    class Meta:
        db_table = 'portfolio_asset'
        indexes = [
            models.Index(fields=['ticker']),
            models.Index(fields=['isin']),
            models.Index(fields=['market']),
        ]

    def __str__(self):
        return f"{self.ticker} ({self.name})"

class Holding(models.Model):
    ASSET_CLASS_CHOICES = [
        ('Equity', 'Equity'),
        ('Fixed Income', 'Fixed Income'),
        ('Cash', 'Cash'),
        ('Alternative', 'Alternative')
    ]
    
    portfolio = models.ForeignKey(Portfolio, on_delete=models.CASCADE, related_name='holdings')
    asset = models.ForeignKey(Asset, on_delete=models.SET_NULL, null=True, related_name='holdings')
    quantity = models.DecimalField(max_digits=20, decimal_places=4)
    book_cost = models.DecimalField(max_digits=20, decimal_places=2)
    market_value = models.DecimalField(max_digits=20, decimal_places=2, null=True)
    asset_class = models.CharField(max_length=20, choices=ASSET_CLASS_CHOICES)
    isin = models.CharField(max_length=12, blank=True, null=True)
    ticker = models.CharField(max_length=10, blank=True, null=True)
    currency = models.CharField(max_length=3, default='USD')
    purchase_date = models.DateField()
    purchase_price = models.DecimalField(max_digits=20, decimal_places=6, null=True)
    last_updated = models.DateTimeField(auto_now=True)
    current_price = models.DecimalField(max_digits=20, decimal_places=6, null=True, blank=True)
    
    class Meta:
        db_table = 'portfolio_holding'
        indexes = [
            models.Index(fields=['portfolio', 'asset_class'], name='holding_port_asset_idx'),
            models.Index(fields=['ticker'], name='holding_ticker_idx'),
            models.Index(fields=['isin'], name='holding_isin_idx'),
            models.Index(fields=['purchase_date'], name='holding_date_idx'),
        ]
        
    def __str__(self):
        return f"{self.ticker or self.isin} ({self.quantity})"

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
        if self.purchase_price is not None:
            self.purchase_price = Decimal(str(self.purchase_price)).quantize(
                Decimal('0.000001'),
                rounding=ROUND_HALF_UP
            )
        super().save(*args, **kwargs)

    def get_value_in_currency(self, target_currency: str, target_date: date = None) -> Optional[Decimal]:
        """
        Get the value of the holding in the specified currency.
        If date is not provided, uses the current date.
        """
        try:
            from .market_data import get_or_fetch_market_price, get_or_fetch_fx_rate
            if target_date is None:
                target_date = timezone.now().date()
            elif isinstance(target_date, datetime):
                target_date = target_date.date()
            
            # For cash holdings, just convert the book cost
            if self.asset_class == 'Cash':
                if self.currency == target_currency:
                    return self.book_cost
                else:
                    fx_rate = get_or_fetch_fx_rate(self.currency, target_currency, target_date)
                    if fx_rate is None:
                        logger.error(f"Could not get FX rate for {self.currency}/{target_currency}")
                        return None
                    return self.book_cost * fx_rate
            
            # For other holdings, get market price and convert
            price = get_or_fetch_market_price(self.ticker, target_date)
            if price is None:
                logger.error(f"Could not get price for {self.ticker} on {target_date}")
                return None
            
            value = price * self.quantity
            
            # Convert to target currency if needed
            if self.currency != target_currency:
                fx_rate = get_or_fetch_fx_rate(self.currency, target_currency, target_date)
                if fx_rate is None:
                    logger.error(f"Could not get FX rate for {self.currency}/{target_currency}")
                    return None
                value = value * fx_rate
            
            return value
            
        except Exception as e:
            logger.error(f"Error getting value in {target_currency} for holding {self.id}: {e}")
            return None

    def update_market_price(self):
        from .market_data import get_or_fetch_market_price
        price = get_or_fetch_market_price(self.ticker, timezone.now().date())
        if price is not None:
            self.current_price = price
            self.market_value = price * self.quantity
            self.save()

class PortfolioNAV(models.Model):
    portfolio = models.ForeignKey(Portfolio, on_delete=models.CASCADE, related_name='nav_records')
    date = models.DateField()
    total_value = models.DecimalField(max_digits=20, decimal_places=2)
    nav = models.DecimalField(max_digits=20, decimal_places=2)
    daily_return = models.DecimalField(max_digits=10, decimal_places=4, null=True)
    last_updated = models.DateTimeField(auto_now=True)
    is_complete = models.BooleanField(default=True)  # True if all holdings had a valid price
    
    class Meta:
        db_table = 'portfolio_nav'
        unique_together = ['portfolio', 'date']
        indexes = [
            models.Index(fields=['portfolio', 'date'], name='nav_port_date_idx'),
            models.Index(fields=['date'], name='nav_date_idx'),
        ]
        ordering = ['-date']
    
    def __str__(self):
        return f"{self.portfolio.name} NAV on {self.date}: {self.nav}"
    
    def save(self, *args, **kwargs):
        """Save the NAV record with proper decimal handling"""
        # Ensure consistent decimal precision
        if self.total_value is not None:
            self.total_value = Decimal(str(self.total_value)).quantize(
                Decimal('0.01'),
                rounding=ROUND_HALF_UP
            )
        if self.nav is not None:
            self.nav = Decimal(str(self.nav)).quantize(
                Decimal('0.01'),
                rounding=ROUND_HALF_UP
            )
        if self.daily_return is not None:
            self.daily_return = Decimal(str(self.daily_return)).quantize(
                Decimal('0.0001'),
                rounding=ROUND_HALF_UP
            )
        super().save(*args, **kwargs)
    
    def calculate_daily_return(self) -> Optional[Decimal]:
        """Calculate the daily return for this NAV record"""
        try:
            # Get previous day's NAV
            prev_nav = PortfolioNAV.objects.filter(
                portfolio=self.portfolio,
                date__lt=self.date
            ).order_by('-date').first()
            
            if not prev_nav or prev_nav.nav == 0:
                return None
            
            # Calculate return
            return (self.nav - prev_nav.nav) / prev_nav.nav
        except Exception as e:
            logger.error(f"Error calculating daily return for {self}: {e}")
            return None

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
        db_table = 'portfolio_holding_return'
        indexes = [
            models.Index(fields=['portfolio', 'date'], name='holding_returns_port_date_idx'),
            models.Index(fields=['holding', 'date'], name='holding_returns_hold_date_idx'),
        ]
        unique_together = [['portfolio', 'holding', 'date']]
        ordering = ['-date']

    def __str__(self):
        return f"{self.portfolio.name} - {self.holding.ticker if self.holding else 'No Holding'} - {self.date}"

    @classmethod
    def calculate_return(cls, current_value: Decimal, previous_value: Decimal) -> Optional[Decimal]:
        if not previous_value or previous_value == 0:
            return None
        return (current_value - previous_value) / previous_value

    @classmethod
    def calculate_and_save(cls, portfolio, holding, date):
        """
        Calculate and save holding return for a specific date
        """
        try:
            # Get previous day's market value
            prev_return = cls.objects.filter(
                portfolio=portfolio,
                holding=holding,
                date__lt=date
            ).order_by('-date').first()

            if not prev_return:
                return None

            # Calculate return
            current_value = holding.market_value
            if not current_value:
                return None

            daily_return = cls.calculate_return(current_value, prev_return.market_value)

            # Create or update the return record
            return_obj, created = cls.objects.update_or_create(
                portfolio=portfolio,  # Ensure portfolio is set
                holding=holding,
                date=date,
                defaults={
                    'daily_return': daily_return,
                    'market_value': current_value
                }
            )

            return return_obj

        except Exception as e:
            logger.error(f"Error calculating holding return: {e}")
            return None

class BenchmarkReturn(models.Model):
    benchmark_name = models.CharField(max_length=50)
    display_name = models.CharField(max_length=100, blank=True, null=True)
    date = models.DateField()
    close_price = models.DecimalField(max_digits=20, decimal_places=6, null=True)
    daily_return = models.DecimalField(max_digits=10, decimal_places=6)
    return_index = models.DecimalField(max_digits=20, decimal_places=6, null=True)

    class Meta:
        db_table = 'portfolio_benchmark_return'
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
        db_table = 'portfolio_rebalance_plan'

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
        db_table = 'portfolio_trade_history'

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
    Legacy model for backward compatibility
    """
    portfolio = models.ForeignKey(Portfolio, on_delete=models.CASCADE, related_name='market_prices')
    ticker = models.CharField(max_length=20)
    date = models.DateField()
    price = models.DecimalField(max_digits=20, decimal_places=6)
    last_updated = models.DateTimeField(auto_now=True)

    class Meta:
        db_table = 'portfolio_market_price_legacy'
        unique_together = ('portfolio', 'ticker', 'date')
        indexes = [
            models.Index(fields=['ticker']),
            models.Index(fields=['date']),
            models.Index(fields=['portfolio', 'ticker']),
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
        db_table = 'portfolio_fx_rate'
        unique_together = ('base_currency', 'target_currency', 'date')
        indexes = [
            models.Index(fields=['base_currency', 'target_currency', 'date']),
        ]

    def __str__(self):
        return f"{self.base_currency}/{self.target_currency} = {self.rate} on {self.date}"

class GlobalMarketPrice(models.Model):
    """
    New global price model
    """
    asset = models.ForeignKey(Asset, on_delete=models.CASCADE, related_name='global_prices')
    date = models.DateField()
    price = models.DecimalField(max_digits=20, decimal_places=6)
    is_trading_day = models.BooleanField(default=True)
    last_updated = models.DateTimeField(auto_now=True)

    class Meta:
        db_table = 'portfolio_global_market_price'
        unique_together = ('asset', 'date')
        indexes = [
            models.Index(fields=['asset', 'date']),
            models.Index(fields=['date']),
            models.Index(fields=['is_trading_day']),
        ]

    def __str__(self):
        return f"{self.asset.ticker} - {self.date}: {self.price}"
