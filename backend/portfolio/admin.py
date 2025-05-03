from django.contrib import admin
from .models import Portfolio, Holding, PortfolioNAV, HoldingReturn, BenchmarkReturn, RebalancePlan, TradeHistory, FXRate

@admin.register(Portfolio)
class PortfolioAdmin(admin.ModelAdmin):
    list_display = ('id', 'name', 'description', 'created_at', 'updated_at')
    search_fields = ('name', 'description')
    ordering = ('-created_at',)

@admin.register(Holding)
class HoldingAdmin(admin.ModelAdmin):
    list_display = ('portfolio', 'ticker', 'quantity', 'currency', 'purchase_date', 
                   'purchase_price', 'current_price', 'last_updated')
    list_filter = ('portfolio', 'currency', 'purchase_date')
    search_fields = ('ticker', 'portfolio__name')
    date_hierarchy = 'purchase_date'

@admin.register(PortfolioNAV)
class PortfolioNAVAdmin(admin.ModelAdmin):
    list_display = ('id', 'portfolio', 'date', 'nav')
    list_filter = ('portfolio', 'date')
    ordering = ('-date',)

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
