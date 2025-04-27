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

    class Meta:
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
            self.quantity = None
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
                
                if current_price:
                    self.current_price = Decimal(str(current_price))
                    self.market_value = self.current_price * self.quantity
                    self.last_price_update = timezone.now()
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

class NAVSnapshot(models.Model):
    portfolio = models.ForeignKey(Portfolio, related_name='nav_snapshots', on_delete=models.CASCADE)
    date = models.DateField()
    nav = models.DecimalField(max_digits=15, decimal_places=6)

    class Meta:
        unique_together = ('portfolio', 'date')
        ordering = ['date']

    def __str__(self):
        return f"{self.portfolio.name} NAV on {self.date}"

class RebalanceSnapshot(models.Model):
    portfolio = models.ForeignKey(Portfolio, on_delete=models.CASCADE, related_name='rebalance_snapshots')
    date_created = models.DateTimeField(auto_now_add=True)
    target_weights = models.JSONField()
    current_weights = models.JSONField()
    action_plan = models.JSONField()
    comment = models.TextField(blank=True, null=True)
    executed = models.BooleanField(default=False)

    def __str__(self):
        return f"RebalanceSnapshot for {self.portfolio.name} at {self.date_created}"
