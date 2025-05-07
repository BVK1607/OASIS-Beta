from django.urls import path, include
from rest_framework.routers import DefaultRouter
from .views import PortfolioViewSet, HoldingViewSet, PortfolioNAVViewSet, TradeHistoryViewSet, BenchmarksViewSet, get_fx_rate, get_portfolio_value, RiskAnalyticsView

# Create a router and register our viewsets with it.
router = DefaultRouter()
router.register(r'portfolios', PortfolioViewSet, basename='portfolio')
router.register(r'holdings', HoldingViewSet, basename='holding')
router.register(r'nav-snapshots', PortfolioNAVViewSet, basename='nav-snapshot')
router.register(r'trade-history', TradeHistoryViewSet, basename='trade-history')
router.register(r'benchmarks', BenchmarksViewSet, basename='benchmarks')

# The API URLs are now determined automatically by the router.
urlpatterns = [
    path('', include(router.urls)),
    path('fx-rates/', get_fx_rate, name='get-fx-rate'),
    path('portfolios/<int:pk>/value/', get_portfolio_value, name='get-portfolio-value'),
    path('api/portfolios/<int:portfolio_id>/risk_analytics/', RiskAnalyticsView.as_view()),
] 