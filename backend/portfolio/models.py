from django.db import models
from django.core.validators import MinValueValidator
from decimal import Decimal
from django.utils import timezone
import yfinance as yf
from django.core.exceptions import ValidationError
from django.contrib.postgres.fields import JSONField

class Portfolio(models.Model):
    RISK_TOLERANCE_CHOICES = [
        ('Conservative', 'Conservative'),
        ('Moderate', 'Moderate'),
        ('Aggressive', 'Aggressive'),
    ]
    name = models.CharField(max_length=100)
    description = models.TextField(blank=True, default='')
    risk_tolerance = models.CharField(max_length=20, choices=RISK_TOLERANCE_CHOICES, default='Moderate')
    target_return = models.DecimalField(max_digits=5, decimal_places=2, blank=True, null=True)
    creation_date = models.DateField(default=timezone.now)
    created_at = models.DateTimeField(auto_now_add=True)
    updated_at = models.DateTimeField(auto_now=True)

    class Meta:
        db_table = 'portfolio'

    def __str__(self):
        return f"{self.name} ({self.risk_tolerance})"
    
    def update_market_prices(self):
        """Update market prices for all holdings in the portfolio"""
        for holding in self.holdings.all():
            if holding.asset_class != 'Cash':  # Skip cash holdings
                holding.update_market_price()
            
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
    ticker = models.CharField(max_length=10, blank=True, null=True)
    quantity = models.DecimalField(
        max_digits=15, 
        decimal_places=4,
        validators=[MinValueValidator(Decimal('0.0001'))],
        default=Decimal('0'),
        blank=True,
        null=True
    )
    book_cost = models.DecimalField(
        max_digits=15,
        decimal_places=2,
        validators=[MinValueValidator(Decimal('0.01'))],
        default=Decimal('0'),
    )
    currency = models.CharField(max_length=3, default='USD')
    trade_date = models.DateField(default=timezone.now)
    
    # Market data fields
    current_price = models.DecimalField(
        max_digits=15,
        decimal_places=2,
        null=True,
        blank=True
    )
    market_value = models.DecimalField(
        max_digits=15,
        decimal_places=2,
        null=True,
        blank=True
    )
    last_price_update = models.DateTimeField(null=True, blank=True)

    created_at = models.DateTimeField(auto_now_add=True)
    updated_at = models.DateTimeField(auto_now=True)

    name = models.CharField(max_length=128, blank=True, null=True)

    class Meta:
        db_table = 'holding'
        # Only enforce unique constraint for non-cash positions
        constraints = [
            models.UniqueConstraint(
                fields=['portfolio', 'isin'],
                condition=~models.Q(asset_class='Cash'),
                name='unique_portfolio_isin_non_cash'
            )
        ]
        ordering = ['-created_at']

    def __str__(self):
        if self.asset_class == 'Cash':
            return f"Cash ({self.currency}) - {self.portfolio.name}"
        return f"{self.ticker} ({self.isin}) - {self.portfolio.name}"

    def clean(self):
        """Custom validation based on asset class"""
        if self.asset_class == 'Cash':
            # For cash, only currency and trade_date are required
            if not self.currency:
                raise ValidationError({'currency': 'Currency is required for cash holdings'})
            if not self.trade_date:
                raise ValidationError({'trade_date': 'Trade date is required for cash holdings'})
            if not self.book_cost:
                raise ValidationError({'book_cost': 'Amount is required for cash holdings'})
            
            # Set market value equal to book cost for cash
            self.market_value = self.book_cost
            self.current_price = self.book_cost
            self.last_price_update = timezone.now()
            
            # Clear other fields for cash positions
            self.isin = None
            self.ticker = None
            self.quantity = 1
        else:
            # For non-cash assets, validate required fields
            if not self.isin:
                raise ValidationError({'isin': 'ISIN is required for non-cash holdings'})
            if not self.ticker:
                raise ValidationError({'ticker': 'Ticker is required for non-cash holdings'})
            if not self.quantity:
                raise ValidationError({'quantity': 'Quantity is required for non-cash holdings'})
            if not self.book_cost:
                raise ValidationError({'book_cost': 'Book cost is required for non-cash holdings'})

    def save(self, *args, **kwargs):
        self.clean()
        super().save(*args, **kwargs)

    def update_market_price(self):
        """Fetch current market price using yfinance"""
        if self.asset_class == 'Cash':
            # For cash, market value equals book cost
            self.current_price = self.book_cost
            self.market_value = self.book_cost
            self.last_price_update = timezone.now()
            return True
        else:
            try:
                # Get stock info
                stock = yf.Ticker(self.ticker)
                current_price = stock.info.get('regularMarketPrice')
                short_name = stock.info.get('shortName')
                if current_price:
                    self.current_price = Decimal(str(current_price))
                    self.market_value = self.current_price * self.quantity
                    self.last_price_update = timezone.now()
                    if short_name:
                        self.name = short_name
                    self.save()
                    return True
            except Exception as e:
                print(f"Error updating price for {self.ticker}: {str(e)}")
        return False

    @property
    def unrealized_gain_loss(self):
        """Calculate unrealized gain/loss"""
        if self.asset_class == 'Cash':
            return Decimal('0')  # Cash positions have no gain/loss
        if self.market_value is not None and self.book_cost is not None and self.quantity is not None:
            return self.market_value - (self.book_cost * self.quantity)
        return None

class PortfolioNAV(models.Model):
    portfolio = models.ForeignKey(Portfolio, related_name='nav_snapshots', on_delete=models.CASCADE)
    date = models.DateField()
    nav = models.DecimalField(max_digits=20, decimal_places=4)
    daily_return = models.DecimalField(max_digits=10, decimal_places=6, null=True)

    class Meta:
        db_table = 'portfolio_nav'
        unique_together = ('portfolio', 'date')
        indexes = [
            models.Index(fields=['portfolio', 'date'], name='nav_port_date_idx'),
        ]
        ordering = ['date']

    def __str__(self):
        return f"{self.portfolio.name} NAV on {self.date}"

class HoldingReturn(models.Model):
    portfolio = models.ForeignKey(Portfolio, on_delete=models.CASCADE)
    ticker = models.CharField(max_length=20)
    date = models.DateField()
    daily_return = models.DecimalField(max_digits=10, decimal_places=6)

    class Meta:
        db_table = 'holding_return'
        unique_together = ('portfolio', 'ticker', 'date')
        indexes = [
            models.Index(fields=['portfolio', 'ticker', 'date'], name='holdings_port_tick_date_idx'),
        ]
        ordering = ['portfolio', 'ticker', 'date']

    def __str__(self):
        return f"{self.ticker} return for {self.portfolio.name} on {self.date}"

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
