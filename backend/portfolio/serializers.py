from rest_framework import serializers
from .models import Portfolio, Holding, PortfolioNAV, RebalancePlan, TradeHistory, HoldingReturn, BenchmarkReturn, FXRate
from decimal import Decimal
from datetime import datetime
import re

class HoldingSerializer(serializers.ModelSerializer):
    purchase_date = serializers.DateField(
        format='%Y-%m-%d',
        input_formats=['%Y-%m-%d', '%d/%m/%Y', '%d-%m-%Y']
    )
    quantity = serializers.DecimalField(max_digits=20, decimal_places=6)
    book_cost = serializers.DecimalField(max_digits=15, decimal_places=2)
    purchase_price = serializers.DecimalField(max_digits=20, decimal_places=6)
    current_price = serializers.DecimalField(max_digits=20, decimal_places=6, required=False)
    market_value = serializers.DecimalField(max_digits=15, decimal_places=2, required=False)
    
    class Meta:
        model = Holding
        fields = [
            'id', 'portfolio', 'asset_class', 'isin', 'ticker', 
            'quantity', 'book_cost', 'currency', 'purchase_date',
            'purchase_price', 'current_price', 'market_value', 
            'last_updated', 'created_at', 'updated_at', 'name'
        ]
        read_only_fields = ['current_price', 'market_value', 'last_updated', 
                          'created_at', 'updated_at']

    def validate_currency(self, value):
        """
        Check that the currency is a valid 3-letter code.
        """
        if not re.match(r'^[A-Z]{3}$', value):
            raise serializers.ValidationError('Currency must be a valid 3-letter code')
        return value

class FXRateSerializer(serializers.ModelSerializer):
    class Meta:
        model = FXRate
        fields = ['id', 'base_currency', 'target_currency', 'rate', 'date', 'last_updated']
        read_only_fields = ['last_updated']

    def validate(self, data):
        """
        Check that base_currency and target_currency are different.
        """
        if data['base_currency'] == data['target_currency']:
            raise serializers.ValidationError('Base currency and target currency must be different')
        return data

    def validate_base_currency(self, value):
        """
        Check that the base currency is a valid 3-letter code.
        """
        if not re.match(r'^[A-Z]{3}$', value):
            raise serializers.ValidationError('Base currency must be a valid 3-letter code')
        return value

    def validate_target_currency(self, value):
        """
        Check that the target currency is a valid 3-letter code.
        """
        if not re.match(r'^[A-Z]{3}$', value):
            raise serializers.ValidationError('Target currency must be a valid 3-letter code')
        return value

class PortfolioNAVSerializer(serializers.ModelSerializer):
    class Meta:
        model = PortfolioNAV
        fields = ['id', 'portfolio', 'date', 'nav', 'daily_return']
        read_only_fields = ['id']

class HoldingReturnSerializer(serializers.ModelSerializer):
    class Meta:
        model = HoldingReturn
        fields = ['id', 'portfolio', 'ticker', 'date', 'daily_return']
        read_only_fields = ['id']

class BenchmarkReturnSerializer(serializers.ModelSerializer):
    class Meta:
        model = BenchmarkReturn
        fields = ['id', 'benchmark_name', 'date', 'daily_return']
        read_only_fields = ['id']

class PortfolioSerializer(serializers.ModelSerializer):
    creation_date = serializers.DateField(
        format='%d/%m/%Y',
        input_formats=['%d/%m/%Y', '%d-%m-%Y', '%Y-%m-%d']
    )
    holdings = HoldingSerializer(many=True, read_only=True)
    nav_snapshots = PortfolioNAVSerializer(many=True, read_only=True)
    total_market_value = serializers.DecimalField(
        max_digits=15, decimal_places=2, read_only=True
    )
    total_book_cost = serializers.DecimalField(
        max_digits=15, decimal_places=2, read_only=True
    )
    total_gain_loss = serializers.DecimalField(
        max_digits=15, decimal_places=2, read_only=True
    )
    target_return = serializers.DecimalField(
        max_digits=5, decimal_places=2, required=False, allow_null=True
    )
    base_currency = serializers.ChoiceField(choices=Portfolio.CURRENCY_CHOICES, default='USD')

    class Meta:
        model = Portfolio
        fields = [
            'id', 'name', 'description', 'risk_tolerance', 'target_return', 'creation_date',
            'holdings', 'nav_snapshots', 'total_market_value', 'total_book_cost', 'total_gain_loss',
            'base_currency'
        ]
        read_only_fields = ['id']

    def get_total_market_value(self, obj):
        """Calculate total market value of all holdings"""
        return sum(
            holding.market_value 
            for holding in obj.holdings.all() 
            if holding.market_value is not None
        )

    def get_total_book_cost(self, obj):
        """Calculate total book cost: (quantity * book_cost) for securities, just book_cost for cash"""
        total = 0
        for holding in obj.holdings.all():
            if holding.asset_class == 'Cash':
                total += holding.book_cost or 0
            else:
                total += (holding.book_cost or 0) * (holding.quantity or 0)
        return total

    def get_total_gain_loss(self, obj):
        return self.get_total_market_value(obj) - self.get_total_book_cost(obj)

    def to_internal_value(self, data):
        # Accept DD-MM-YYYY, DD/MM/YYYY, or YYYY-MM-DD for creation_date
        creation_date = data.get('creation_date')
        if isinstance(creation_date, str):
            for fmt in ('%d-%m-%Y', '%d/%m/%Y', '%Y-%m-%d'):
                try:
                    data['creation_date'] = datetime.strptime(creation_date, fmt).date()
                    break
                except ValueError:
                    continue
        # Handle target_return as optional/nullable
        target_return = data.get('target_return')
        if target_return in (None, '', 'null'):
            data['target_return'] = None
        else:
            try:
                data['target_return'] = Decimal(str(target_return))
            except Exception:
                raise serializers.ValidationError({'target_return': 'Target return must be a number or blank.'})
        return super().to_internal_value(data)

    def to_representation(self, instance):
        data = super().to_representation(instance)
        for field in ['total_market_value', 'total_book_cost', 'total_gain_loss']:
            value = data.get(field)
            if value is not None:
                data[field] = float(value)
        return data 

class RebalancePlanSerializer(serializers.ModelSerializer):
    class Meta:
        model = RebalancePlan
        fields = '__all__'

class TradeHistorySerializer(serializers.ModelSerializer):
    date = serializers.DateTimeField(format='%d/%m/%Y, %H:%M', read_only=True)
    class Meta:
        model = TradeHistory
        fields = [
            'id', 'portfolio', 'date', 'trade_type', 'ticker', 'isin', 'asset_class',
            'quantity', 'price_at_trade', 'book_cost', 'gross_amount', 'cash_flow_impact',
            'currency', 'portfolio_weight_change', 'notes'
        ]
        read_only_fields = ['id', 'date', 'gross_amount', 'cash_flow_impact']

class BenchmarkListSerializer(serializers.ModelSerializer):
    class Meta:
        model = BenchmarkReturn
        fields = ['benchmark_name', 'display_name']

class BenchmarkReturnChartSerializer(serializers.ModelSerializer):
    date = serializers.DateField(format='%d/%m/%Y')
    class Meta:
        model = BenchmarkReturn
        fields = ['date', 'return_index', 'close_price', 'daily_return'] 