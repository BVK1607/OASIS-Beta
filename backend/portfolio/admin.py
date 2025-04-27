from django.contrib import admin
from .models import Portfolio, Holding, NAVSnapshot

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

@admin.register(NAVSnapshot)
class NAVSnapshotAdmin(admin.ModelAdmin):
    list_display = ('id', 'portfolio', 'date', 'nav')
    list_filter = ('portfolio', 'date')
    ordering = ('-date',)
