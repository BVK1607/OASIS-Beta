from rest_framework import serializers
from .models import Portfolio, Holding, NAVSnapshot, RebalanceSnapshot
from decimal import Decimal
from datetime import datetime

class HoldingSerializer(serializers.ModelSerializer):
    trade_date = serializers.DateField(
        format='%d-%m-%Y',
        input_formats=['%d-%m-%Y', '%d/%m/%Y', '%Y-%m-%d']
    )
    class Meta:
        model = Holding
        fields = [
            'id', 'portfolio', 'asset_class', 'isin', 'ticker',
            'quantity', 'book_cost', 'currency', 'trade_date',
            'current_price', 'market_value', 'last_price_update'
        ]
        read_only_fields = [
            'id', 'current_price', 'market_value', 'last_price_update'
        ]

    def validate(self, data):
        """
        Validate based on asset class
        """
        asset_class = data.get('asset_class')
        
        if asset_class == 'Cash':
            # For cash positions, only validate currency and trade_date
            if not data.get('currency'):
                raise serializers.ValidationError({'currency': 'Currency is required for cash holdings'})
            if not data.get('trade_date'):
                raise serializers.ValidationError({'trade_date': 'Trade date is required for cash holdings'})
            
            # For cash, book_cost is the amount of cash
            if not data.get('book_cost'):
                raise serializers.ValidationError({'book_cost': 'Amount is required for cash holdings'})
            
            # Clear other fields for cash positions
            data['isin'] = None
            data['ticker'] = None
            data['quantity'] = None
            
        else:
            # For non-cash assets, validate all required fields
            self.validate_isin(data.get('isin'))
            self.validate_ticker(data.get('ticker'))
            self.validate_quantity(data.get('quantity'))
            self.validate_book_cost(data.get('book_cost'))
            self.validate_currency(data.get('currency'))
            
        return data

    def validate_isin(self, value):
        """Validate ISIN format"""
        if value is None:
            return value
        if not value:
            raise serializers.ValidationError("ISIN is required for non-cash holdings")
        if len(value) != 12:
            raise serializers.ValidationError("ISIN must be exactly 12 characters long")
        return value.upper()

    def validate_ticker(self, value):
        """Validate ticker format"""
        if value is None:
            return value
        if not value:
            raise serializers.ValidationError("Ticker is required for non-cash holdings")
        if len(value) > 10:
            raise serializers.ValidationError("Ticker must be 10 characters or less")
        return value.upper()

    def validate_quantity(self, value):
        """Validate quantity is positive"""
        if value is None:
            return value
        try:
            quantity = Decimal(str(value))
            if quantity <= 0:
                raise serializers.ValidationError("Quantity must be greater than 0")
            return quantity
        except (TypeError, ValueError):
            raise serializers.ValidationError("Quantity must be a valid number")

    def validate_book_cost(self, value):
        """Validate book cost is positive"""
        if value is None:
            return value
        try:
            book_cost = Decimal(str(value))
            if book_cost <= 0:
                raise serializers.ValidationError("Amount/Book cost must be greater than 0")
            return book_cost
        except (TypeError, ValueError):
            raise serializers.ValidationError("Amount/Book cost must be a valid number")

    def validate_currency(self, value):
        """Validate currency format"""
        if not value:
            raise serializers.ValidationError("Currency is required")
        if len(value) != 3:
            raise serializers.ValidationError("Currency must be a 3-letter code")
        return value.upper()

    def validate_asset_class(self, value):
        """Validate asset class"""
        if not value:
            raise serializers.ValidationError("Asset class is required")
        valid_classes = ['Equity', 'Fixed Income', 'Alternatives', 'Cash']
        if value not in valid_classes:
            raise serializers.ValidationError(f"Asset class must be one of: {', '.join(valid_classes)}")
        return value

    def validate_trade_date(self, value):
        """Validate trade date"""
        if not value:
            raise serializers.ValidationError("Trade date is required")
        return value

    def to_internal_value(self, data):
        # Accept DD-MM-YYYY, DD/MM/YYYY, or YYYY-MM-DD for trade_date
        trade_date = data.get('trade_date')
        if isinstance(trade_date, str):
            for fmt in ('%d-%m-%Y', '%d/%m/%Y', '%Y-%m-%d'):
                try:
                    data['trade_date'] = datetime.strptime(trade_date, fmt).date()
                    break
                except ValueError:
                    continue
        return super().to_internal_value(data)

class NAVSnapshotSerializer(serializers.ModelSerializer):
    class Meta:
        model = NAVSnapshot
        fields = ['id', 'portfolio', 'date', 'nav']

class PortfolioSerializer(serializers.ModelSerializer):
    creation_date = serializers.DateField(
        format='%d-%m-%Y',
        input_formats=['%d-%m-%Y', '%d/%m/%Y', '%Y-%m-%d']
    )
    holdings = HoldingSerializer(many=True, read_only=True)
    nav_snapshots = NAVSnapshotSerializer(many=True, read_only=True)
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

    class Meta:
        model = Portfolio
        fields = [
            'id', 'name', 'description', 'risk_tolerance', 'target_return', 'creation_date',
            'holdings', 'nav_snapshots', 'total_market_value', 'total_book_cost', 'total_gain_loss'
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

class RebalanceSnapshotSerializer(serializers.ModelSerializer):
    class Meta:
        model = RebalanceSnapshot
        fields = [
            'id', 'portfolio', 'date_created', 'target_weights', 'current_weights',
            'action_plan', 'comment', 'executed'
        ]

    def validate(self, data):
        target_weights = data.get('target_weights', {})
        current_weights = data.get('current_weights', {})
        target_sum = sum(target_weights.values())
        current_sum = sum(current_weights.values())
        if abs(target_sum - 100) > 0.5:
            raise serializers.ValidationError({'target_weights': 'Target weights must sum to approximately 100%.'})
        if abs(current_sum - 100) > 0.5:
            raise serializers.ValidationError({'current_weights': 'Current weights must sum to approximately 100%.'})
        return data 