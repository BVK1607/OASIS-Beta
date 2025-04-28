from django.contrib import admin
from .models import Portfolio, Holding, PortfolioNAV, HoldingReturn, BenchmarkReturn, RebalancePlan, TradeHistory

@admin.register(Portfolio)
class PortfolioAdmin(admin.ModelAdmin):
    list_display = ('id', 'name', 'description', 'created_at', 'updated_at')
    search_fields = ('name', 'description')
    ordering = ('-created_at',)

@admin.register(Holding)
class HoldingAdmin(admin.ModelAdmin):
    list_display = ('id', 'portfolio', 'asset_class', 'isin', 'ticker', 'quantity', 
                   'book_cost', 'currency', 'trade_date', 'current_price', 'market_value')
    list_filter = ('asset_class', 'currency', 'portfolio')
    search_fields = ('isin', 'ticker')
    ordering = ('-created_at',)

@admin.register(PortfolioNAV)
class PortfolioNAVAdmin(admin.ModelAdmin):
    list_display = ('id', 'portfolio', 'date', 'nav')
    list_filter = ('portfolio', 'date')
    ordering = ('-date',)

@admin.register(HoldingReturn)
class HoldingReturnAdmin(admin.ModelAdmin):
    list_display = ('id', 'portfolio', 'ticker', 'date', 'daily_return')
    list_filter = ('portfolio', 'ticker', 'date')
    ordering = ('portfolio', 'ticker', 'date')

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
