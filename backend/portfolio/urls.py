from django.urls import path, include
from rest_framework.routers import DefaultRouter
from .views import PortfolioViewSet, HoldingViewSet, NAVSnapshotViewSet

# Create a router and register our viewsets with it.
router = DefaultRouter()
router.register(r'portfolios', PortfolioViewSet, basename='portfolio')
router.register(r'holdings', HoldingViewSet, basename='holding')
router.register(r'nav-snapshots', NAVSnapshotViewSet, basename='nav-snapshot')

# The API URLs are now determined automatically by the router.
urlpatterns = [
    path('', include(router.urls)),
] 