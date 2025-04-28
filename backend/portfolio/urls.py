from django.urls import path, include
from rest_framework.routers import DefaultRouter
from .views import PortfolioViewSet, HoldingViewSet, PortfolioNAVViewSet, TradeHistoryViewSet, BenchmarksViewSet

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
] 