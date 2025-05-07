from django.contrib import admin
from .models import Portfolio, Holding, Asset, GlobalMarketPrice, PortfolioNAV, HoldingReturn, BenchmarkReturn, RebalancePlan, TradeHistory, FXRate

@admin.register(Portfolio)
class PortfolioAdmin(admin.ModelAdmin):
    list_display = ['name', 'risk_tolerance', 'base_currency', 'creation_date', 'total_value']
    list_filter = ['risk_tolerance', 'base_currency']
    search_fields = ['name', 'description']
    ordering = ['creation_date']

@admin.register(Holding)
class HoldingAdmin(admin.ModelAdmin):
    list_display = ['portfolio', 'asset', 'quantity', 'book_cost', 'market_value', 'asset_class']
    list_filter = ['asset_class', 'portfolio']
    search_fields = ['ticker', 'isin']

@admin.register(Asset)
class AssetAdmin(admin.ModelAdmin):
    list_display = ['ticker', 'name', 'asset_class', 'currency', 'market']
    list_filter = ['asset_class', 'currency', 'market']
    search_fields = ['ticker', 'name', 'isin']

@admin.register(GlobalMarketPrice)
class GlobalMarketPriceAdmin(admin.ModelAdmin):
    list_display = ['asset', 'date', 'price', 'is_trading_day', 'last_updated']
    list_filter = ['is_trading_day', 'date']
    search_fields = ['asset__ticker']
    ordering = ['-date']

@admin.register(PortfolioNAV)
class PortfolioNAVAdmin(admin.ModelAdmin):
    list_display = ['portfolio', 'date', 'total_value', 'nav', 'daily_return', 'last_updated']
    list_filter = ['portfolio', 'date']
    search_fields = ['portfolio__name']
    ordering = ['-date']

@admin.register(HoldingReturn)
class HoldingReturnAdmin(admin.ModelAdmin):
    list_display = ('id', 'portfolio', 'get_ticker', 'date', 'daily_return')
    list_filter = ('portfolio', 'date')
    ordering = ('portfolio', 'date')
    
    def get_ticker(self, obj):
        return obj.holding.ticker if obj.holding else None
    get_ticker.short_description = 'Ticker'
    get_ticker.admin_order_field = 'holding__ticker'

@admin.register(BenchmarkReturn)
class BenchmarkReturnAdmin(admin.ModelAdmin):
    list_display = ('id', 'benchmark_name', 'date', 'daily_return')
    list_filter = ('benchmark_name', 'date')
    ordering = ('benchmark_name', 'date')

@admin.register(RebalancePlan)
class RebalancePlanAdmin(admin.ModelAdmin):
    list_display = ('id', 'portfolio', 'date_created', 'executed')
    list_filter = ('portfolio', 'executed')
    ordering = ('-date_created',)

@admin.register(TradeHistory)
class TradeHistoryAdmin(admin.ModelAdmin):
    list_display = ('id', 'portfolio', 'date', 'trade_type', 'ticker', 'quantity', 'price_at_trade', 'book_cost', 'gross_amount', 'cash_flow_impact', 'currency')
    list_filter = ('portfolio', 'trade_type', 'currency')
    search_fields = ('ticker', 'isin')
    ordering = ('-date',)

@admin.register(FXRate)
class FXRateAdmin(admin.ModelAdmin):
    list_display = ('base_currency', 'target_currency', 'rate', 'date', 'last_updated')
    list_filter = ('base_currency', 'target_currency', 'date')
    search_fields = ('base_currency', 'target_currency')
    date_hierarchy = 'date'
