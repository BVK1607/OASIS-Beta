from django.shortcuts import render
from rest_framework import viewsets, status
from rest_framework.response import Response
from rest_framework.decorators import action
from django.shortcuts import get_object_or_404
from .models import Portfolio, Holding, PortfolioNAV, RebalancePlan, TradeHistory, HoldingReturn, BenchmarkReturn, Asset
from .serializers import PortfolioSerializer, HoldingSerializer, PortfolioNAVSerializer, RebalancePlanSerializer, TradeHistorySerializer, HoldingReturnSerializer, BenchmarkListSerializer, BenchmarkReturnChartSerializer
from .market_data import get_market_price, get_or_fetch_fx_rate, PriceValidationError, update_market_prices, get_or_fetch_market_price, fetch_and_store_historical_prices
from datetime import datetime, timedelta, date
from django.db import transaction
import io
import logging
from django.utils import timezone
from decimal import Decimal
from collections import defaultdict
from rest_framework import filters
from rest_framework.permissions import AllowAny
import numpy as np
import math
import pandas as pd
from django.core.exceptions import ValidationError
from .task_queue import task_queue
from django.db.utils import OperationalError
import time
from .signals import ensure_latest_data
from django.core.cache import cache
from django.utils.decorators import method_decorator
from django.views.decorators.cache import cache_page
from rest_framework.pagination import PageNumberPagination
from django.db.models import Prefetch, F, Sum
import yfinance as yf
from rest_framework.decorators import api_view, permission_classes
from rest_framework.permissions import IsAuthenticated
from .services import PortfolioNAVService
from rest_framework.views import APIView

logger = logging.getLogger(__name__)

MAX_RETRIES = 3
RETRY_DELAY = 1  # seconds

class StandardResultsSetPagination(PageNumberPagination):
    page_size = 20
    page_size_query_param = 'page_size'
    max_page_size = 100

# Create your views here.

class PortfolioViewSet(viewsets.ModelViewSet):
    """
    ViewSet for Portfolio model operations.
    Provides CRUD operations and custom actions.
    """
    queryset = Portfolio.objects.prefetch_related(
        'holdings',
        'nav_records'
    ).all()
    serializer_class = PortfolioSerializer
    pagination_class = None  # Disable pagination for portfolio list
    permission_classes = [AllowAny]  # Allow all access

    def list(self, request, *args, **kwargs):
        try:
            queryset = self.get_queryset()
            serializer = self.get_serializer(queryset, many=True)
            return Response(serializer.data)
        except Exception as e:
            logger.error(f"Error in portfolio list: {str(e)}")
            return Response(
                {'error': 'Failed to fetch portfolios'},
                status=status.HTTP_500_INTERNAL_SERVER_ERROR
            )

    @method_decorator(cache_page(60 * 5))  # Cache for 5 minutes
    def retrieve(self, request, *args, **kwargs):
        """Get a single portfolio with latest data"""
        try:
            portfolio = self.get_object()
            serializer = self.get_serializer(portfolio)
            return Response(serializer.data)
        except Exception as e:
            logger.error(f"Error retrieving portfolio: {str(e)}")
            return Response(
                {'error': f'Failed to retrieve portfolio: {str(e)}'},
                status=status.HTTP_500_INTERNAL_SERVER_ERROR
            )

    def create(self, request, *args, **kwargs):
        """Override create to handle historical data processing in queue and NAV backfill"""
        try:
            with transaction.atomic():
                response = super().create(request, *args, **kwargs)
                portfolio = Portfolio.objects.get(id=response.data['id'])
                # If there are initial holdings, process them
                if 'holdings' in request.data:
                    holdings = []
                    for holding_data in request.data['holdings']:
                        holding_data['portfolio'] = portfolio.id
                        serializer = HoldingSerializer(data=holding_data)
                        if serializer.is_valid():
                            holdings.append(serializer.save())
                    if holdings:
                        task_queue.add_historical_data_task(portfolio.id, holdings)
                        response.data['message'] = 'Portfolio created. Historical data processing queued.'
                # Fetch historical prices for all tickers in the portfolio
                holdings = portfolio.holdings.all()
                if holdings:
                    today = timezone.now().date()
                    ticker_dates = {}
                    for holding in holdings:
                        if holding.ticker not in ticker_dates:
                            ticker_dates[holding.ticker] = holding.purchase_date
                        else:
                            ticker_dates[holding.ticker] = min(ticker_dates[holding.ticker], holding.purchase_date)
                    for ticker, start_date in ticker_dates.items():
                        fetch_and_store_historical_prices(ticker, start_date, today)
                    # Backfill NAV and holding returns
                    PortfolioNAVService.backfill_portfolio_time_series(portfolio)
                return response
        except Exception as e:
            logger.error(f"Error creating portfolio: {str(e)}")
            return Response(
                {'error': f'Failed to create portfolio: {str(e)}'},
                status=status.HTTP_500_INTERNAL_SERVER_ERROR
            )

    @action(detail=True, methods=['post'])
    def add_holding(self, request, pk=None):
        portfolio = self.get_object()
        
        try:
            logger.info(f"Received holding data: {request.data}")
            
            # Create the holding first
            holding_data = {
                'portfolio': portfolio.id,
                'asset_class': request.data.get('asset_class'),
                'isin': request.data.get('isin'),
                'ticker': request.data.get('ticker'),
                'quantity': Decimal(str(request.data.get('quantity', '0'))),
                'book_cost': Decimal(str(request.data.get('book_cost', '0'))),
                'currency': request.data.get('currency'),
                'purchase_date': request.data.get('purchase_date'),
                'purchase_price': Decimal(str(request.data.get('purchase_price', '0')))
            }
            
            logger.info(f"Processed holding data: {holding_data}")
            
            serializer = HoldingSerializer(data=holding_data)
            if serializer.is_valid():
                logger.info("Holding data is valid")
                holding = serializer.save()
                
                # Fetch current market price
                price = get_market_price(holding.ticker)
                if price is not None:
                    holding.current_price = price
                    holding.market_value = price * holding.quantity
                    holding.last_updated = timezone.now()
                    holding.save()
                    logger.info(f"Updated market price: {price}")
                
                return Response(
                    HoldingSerializer(holding).data,
                    status=status.HTTP_201_CREATED
                )
            else:
                logger.error(f"Validation errors: {serializer.errors}")
                return Response(serializer.errors, status=status.HTTP_400_BAD_REQUEST)
            
        except Exception as e:
            logger.error(f"Error adding holding: {str(e)}")
            return Response(
                {'error': str(e)},
                status=status.HTTP_500_INTERNAL_SERVER_ERROR
            )

    def get_currency_for_ticker(self, ticker):
        try:
            info = yf.Ticker(ticker).info
            return info.get('currency', 'USD')
        except Exception:
            return 'USD'

    @action(detail=True, methods=['post'])
    def upload_csv(self, request, pk=None):
        portfolio = self.get_object()
        file = request.FILES.get('file')
        
        if not file:
            return Response({'error': 'No file provided'}, status=status.HTTP_400_BAD_REQUEST)
        
        if not file.name.endswith('.csv'):
            return Response({'error': 'File must be a CSV'}, status=status.HTTP_400_BAD_REQUEST)

        try:
            # Read CSV file
            content = file.read().decode('utf-8')
            df = pd.read_csv(io.StringIO(content))
            
            # Clean column names
            df.columns = df.columns.str.strip().str.lower().str.replace(' ', '_')
            
            # Validate required columns based on format
            modern_format = all(col in df.columns for col in ['asset_class', 'isin', 'ticker', 'quantity', 'book_cost', 'purchase_date'])
            legacy_format = all(col in df.columns for col in ['isin', 'ticker', 'quantity', 'book_cost', 'purchase_date'])
            
            if not (modern_format or legacy_format):
                return Response({
                    'error': 'Invalid CSV format. Required columns missing.',
                    'modern_format': 'asset_class,isin,ticker,quantity,book_cost,purchase_date',
                    'legacy_format': 'isin,ticker,quantity,book_cost,purchase_date'
                }, status=status.HTTP_400_BAD_REQUEST)

            # Process each row
            holdings_to_create = []
            errors = []
            
            for index, row in df.iterrows():
                try:
                    # Clean data
                    row = row.copy()
                    for col in row.index:
                        if isinstance(row[col], str):
                            row[col] = row[col].strip().upper()
                    
                    # Validate numeric fields
                    try:
                        quantity = float(row['quantity'])
                        book_cost = float(row['book_cost'])
                    except (ValueError, TypeError):
                        errors.append(f"Row {index + 1}: Invalid numeric value for quantity or book_cost")
                        continue
                    
                    # Validate date format
                    try:
                        purchase_date = pd.to_datetime(row['purchase_date']).date()
                    except (ValueError, TypeError):
                        errors.append(f"Row {index + 1}: Invalid date format for purchase_date")
                        continue
                    
                    # Auto-detect currency if not provided
                    currency = row.get('currency')
                    if not currency or currency == '':
                        currency = self.get_currency_for_ticker(row['ticker'])
                    
                    # Create holding data
                    holding_data = {
                        'portfolio': portfolio,
                        'isin': row['isin'],
                        'ticker': row['ticker'],
                        'quantity': quantity,
                        'book_cost': book_cost,
                        'purchase_date': purchase_date,
                        'purchase_price': book_cost / quantity if quantity > 0 else 0,
                        'asset_class': row.get('asset_class', 'Equity'),  # Default to Equity for legacy format
                        'currency': currency,
                    }
                    
                    holdings_to_create.append(holding_data)
                    
                except Exception as e:
                    errors.append(f"Row {index + 1}: {str(e)}")
            
            if errors:
                return Response({'errors': errors}, status=status.HTTP_400_BAD_REQUEST)
            
            # Bulk create holdings with retry mechanism
            retries = 0
            while retries < MAX_RETRIES:
                try:
                    with transaction.atomic():
                        for holding_data in holdings_to_create:
                            # Use update_or_create to handle duplicates
                            Holding.objects.update_or_create(
                                portfolio=holding_data['portfolio'],
                                isin=holding_data['isin'],
                                ticker=holding_data['ticker'],
                                defaults={
                                    'quantity': holding_data['quantity'],
                                    'book_cost': holding_data['book_cost'],
                                    'purchase_date': holding_data['purchase_date'],
                                    'purchase_price': holding_data['purchase_price'],
                                    'asset_class': holding_data['asset_class'],
                                    'currency': holding_data['currency'],
                                }
                            )
                    break  # Success, exit retry loop
                except OperationalError as e:
                    if "database is locked" in str(e).lower():
                        retries += 1
                        if retries < MAX_RETRIES:
                            logger.warning(f"Database locked during bulk create, retrying in {RETRY_DELAY} seconds... (attempt {retries}/{MAX_RETRIES})")
                            time.sleep(RETRY_DELAY)
                        else:
                            logger.error(f"Failed to create holdings after {MAX_RETRIES} retries due to database locks")
                            raise
                    else:
                        raise
            # Fetch historical prices for all tickers in the portfolio
            holdings = portfolio.holdings.all()
            if holdings:
                today = timezone.now().date()
                ticker_dates = {}
                for holding in holdings:
                    if holding.ticker not in ticker_dates:
                        ticker_dates[holding.ticker] = holding.purchase_date
                    else:
                        ticker_dates[holding.ticker] = min(ticker_dates[holding.ticker], holding.purchase_date)
                for ticker, start_date in ticker_dates.items():
                    fetch_and_store_historical_prices(ticker, start_date, today)
                # Backfill NAV and holding returns
                PortfolioNAVService.backfill_portfolio_time_series(portfolio)
            return Response({
                'message': f'Successfully uploaded {len(holdings_to_create)} holdings, fetched historical prices, and updated NAV/returns',
                'holdings_created': len(holdings_to_create)
            })
            
        except Exception as e:
            logger.error(f"Error processing CSV upload: {e}")
            return Response({'error': str(e)}, status=status.HTTP_500_INTERNAL_SERVER_ERROR)

    @action(detail=True, methods=['get'])
    def overview(self, request, pk=None):
        """Get portfolio overview with caching"""
        cache_key = f'portfolio_overview_{pk}'
        cached_data = cache.get(cache_key)
        
        if cached_data is None:
            portfolio = self.get_object()
            
            # Calculate totals using aggregation without triggering price updates
            totals = portfolio.holdings.aggregate(
                total_market_value=Sum('market_value'),
                total_book_cost=Sum('book_cost')
            )
            
            data = {
                'total_market_value': totals['total_market_value'] or 0,
                'total_book_cost': totals['total_book_cost'] or 0,
                'total_gain_loss': (totals['total_market_value'] or 0) - (totals['total_book_cost'] or 0),
                'holdings_count': portfolio.holdings.count(),
                'last_updated': timezone.now()
            }
            
            cache.set(cache_key, data, 60 * 5)  # Cache for 5 minutes
            return Response(data)
        
        return Response(cached_data)

    @action(detail=True, methods=['post'])
    def update_prices(self, request, pk=None):
        """Update prices for all holdings with transaction safety"""
        try:
            portfolio = self.get_object()
            updated_count = 0
            failed_count = 0
            errors = []
            
            with transaction.atomic():
                holdings = portfolio.holdings.select_for_update().all()
                for holding in holdings:
                    try:
                        if holding.update_market_price():
                            updated_count += 1
                        else:
                            failed_count += 1
                            if holding.ticker:  # Only log errors for non-cash holdings with tickers
                                errors.append(f"Failed to update price for {holding.ticker}")
                    except Exception as e:
                        logger.error(f"Error updating price for holding {holding.id}: {str(e)}")
                        failed_count += 1
                        errors.append(f"Error updating {holding.ticker}: {str(e)}")
            
            response_data = {
                'message': f'Updated {updated_count} holdings, {failed_count} failed',
                'updated_count': updated_count,
                'failed_count': failed_count
            }
            
            if errors:
                response_data['errors'] = errors
                
            if failed_count > 0:
                return Response(response_data, status=status.HTTP_206_PARTIAL_CONTENT)
            return Response(response_data)
            
        except Exception as e:
            logger.error(f"Error in update_prices: {str(e)}")
            return Response(
                {'error': str(e)},
                status=status.HTTP_500_INTERNAL_SERVER_ERROR
            )

    @action(detail=True, methods=['get'])
    def performance(self, request, pk=None):
        portfolio = self.get_object()
        data = {
            'total_market_value': portfolio.total_market_value,
            'total_book_cost': portfolio.total_book_cost,
            'total_gain_loss': portfolio.total_gain_loss,
        }
        return Response(data)

    @action(detail=True, methods=['get'])
    def asset_allocation(self, request, pk=None):
        """Get asset allocation with caching"""
        cache_key = f'portfolio_asset_allocation_{pk}'
        cached_data = cache.get(cache_key)
        
        if cached_data is None:
            portfolio = self.get_object()
            
            # Use aggregation for better performance without triggering updates
            allocation = portfolio.holdings.values('asset_class').annotate(
                total_value=Sum('market_value')
            ).order_by('-total_value')
            
            total_value = sum(item['total_value'] or 0 for item in allocation)
            
            data = {
                'allocation': [
                    {
                        'name': item['asset_class'],
                        'value': item['total_value'] or 0,
                        'weight': (item['total_value'] or 0) / total_value if total_value > 0 else 0
                    }
                    for item in allocation
                ],
                'total_value': total_value
            }
            
            cache.set(cache_key, data, 60 * 5)  # Cache for 5 minutes
            return Response(data)
        
        return Response(cached_data)

    @action(detail=True, methods=['get'])
    def currency_exposure(self, request, pk=None):
        """Calculate currency exposure breakdown by currency"""
        portfolio = self.get_object()
        holdings = portfolio.holdings.all()
        
        # Initialize allocation dictionary
        allocation = defaultdict(lambda: Decimal('0'))
        total_value = Decimal('0')
        
        # Calculate total value and sum by currency
        for holding in holdings:
            if holding.market_value:
                allocation[holding.currency] += holding.market_value
                total_value += holding.market_value
        
        # Calculate percentages
        allocation_percentages = []
        for currency, value in allocation.items():
            percentage = (value / total_value * 100) if total_value > 0 else Decimal('0')
            allocation_percentages.append({
                'name': currency,
                'value': round(percentage, 2),
                'market_value': round(value, 2)
            })
        
        return Response({
            'total_value': round(total_value, 2),
            'allocation': allocation_percentages
        })

    def calculate_nav(self, portfolio):
        """Calculate and save NAV for the portfolio"""
        holdings = portfolio.holdings.all()
        if not holdings:
            logger.warning(f"No holdings found for portfolio {portfolio.id}")
            return
            
        try:
            # Find earliest trade date
            start_date = min([h.purchase_date for h in holdings])
            end_date = datetime.today().date()
            date_range = pd.date_range(start=start_date, end=end_date, freq='D')
            
            logger.info(f"Calculating NAV for portfolio {portfolio.id} from {start_date} to {end_date}")
            
            nav_history = []
            for date in date_range:
                total_value = Decimal('0')
                for holding in holdings:
                    try:
                        if holding.asset_class == 'Cash':
                            if holding.purchase_date <= date.date():
                                total_value += holding.book_cost
                            continue
                            
                        if holding.purchase_date > date.date():
                            continue
                            
                        market_data = get_market_price(holding.ticker)
                        if market_data is not None:
                            price = Decimal(str(market_data['price']))
                            total_value += price * holding.quantity
                        else:
                            logger.warning(f"Could not get price for {holding.ticker} on {date.date()}")
                            
                    except Exception as e:
                        logger.error(f"Error processing holding {holding.ticker} for date {date.date()}: {e}")
                    continue
                
                nav_history.append({
                    'date': date.date(),
                    'nav': total_value
                })
            
            # Store NAV snapshots
            with transaction.atomic():
                # First, delete existing NAV records
                PortfolioNAV.objects.filter(portfolio=portfolio).delete()
                
                # Then create new ones
                nav_objects = [
                    PortfolioNAV(
                        portfolio=portfolio,
                        date=entry['date'],
                        nav=entry['nav']
                    ) for entry in nav_history if entry['nav'] > 0
                ]
                
                PortfolioNAV.objects.bulk_create(nav_objects)
                
            logger.info(f"Successfully saved {len(nav_objects)} NAV records for portfolio {portfolio.id}")
            
        except Exception as e:
            logger.error(f"Error calculating NAV for portfolio {portfolio.id}: {e}")
            raise

    @action(detail=True, methods=['post'])
    def rebalance_preview(self, request, pk=None):
        """Calculate rebalancing suggestions for the portfolio"""
        portfolio = self.get_object()
        targets = request.data.get('targets', {})
        
        # Fetch holdings with select_related to reduce queries
        holdings = portfolio.holdings.all().select_related('portfolio')
        
        # Pre-calculate total value and validate prices in one pass
        total_value = Decimal('0')
        missing_prices = False
        holdings_data = []
        
        for holding in holdings:
            if holding.market_value is None or holding.current_price is None or holding.current_price == 0 or holding.market_value == 0:
                missing_prices = True
                break
            
            total_value += holding.market_value
            holdings_data.append({
                'holding': holding,
                'current_value': holding.market_value,
                'current_price': holding.current_price,
                'quantity': holding.quantity
            })

        if missing_prices:
            return Response({
                'error': 'One or more holdings have missing or zero price. Please check tickers and refresh prices.'
            }, status=status.HTTP_400_BAD_REQUEST)

        # Calculate suggestions in one pass
        suggestions = []
        for data in holdings_data:
            holding = data['holding']
            current_value = data['current_value']
            current_weight = (current_value / total_value * 100) if total_value > 0 else 0
            target_weight = float(targets.get(str(holding.id), current_weight))
            target_value = total_value * (Decimal(str(target_weight)) / Decimal('100'))
            target_units = target_value / data['current_price']
            diff_units = target_units - data['quantity']
            
            # Only add to suggestions if there's a meaningful change
            if abs(diff_units) > 1e-6:
                suggestions.append({
                    "ticker": holding.ticker,
                    "action": "BUY" if diff_units > 0 else "SELL",
                    "units": round(abs(diff_units), 2),
                    "current_weight": round(current_weight, 2),
                    "target_weight": round(target_weight, 2),
                    "current_units": data['quantity'],
                    "suggested_units": round(target_units, 2),
                    "difference": round(float(target_weight) - float(current_weight), 2),
                })
            else:
                suggestions.append({
                    "ticker": holding.ticker,
                    "action": "HOLD",
                    "units": 0,
                    "current_weight": round(current_weight, 2),
                    "target_weight": round(target_weight, 2),
                    "current_units": data['quantity'],
                    "suggested_units": round(data['quantity'], 2),
                    "difference": 0,
                })

        return Response({"suggestions": suggestions})

    @action(detail=True, methods=['post'])
    def rebalance_snapshot(self, request, pk=None):
        portfolio = self.get_object()
        data = request.data.copy()
        data['portfolio'] = portfolio.id
        serializer = RebalancePlanSerializer(data=data)
        if serializer.is_valid():
            snapshot = serializer.save()
            return Response(RebalancePlanSerializer(snapshot).data, status=status.HTTP_201_CREATED)
        else:
            return Response(serializer.errors, status=status.HTTP_400_BAD_REQUEST)

    @action(detail=True, methods=['get'])
    def rebalance_snapshots(self, request, pk=None):
        portfolio = self.get_object()
        snapshots = RebalancePlan.objects.filter(portfolio=portfolio).order_by('-date_created')
        serializer = RebalancePlanSerializer(snapshots, many=True)
        return Response(serializer.data)

    @action(detail=True, methods=['get'])
    def nav_history(self, request, pk=None):
        """Get NAV history for a portfolio"""
        portfolio = self.get_object()
        ensure_latest_data(portfolio)  # Ensure data is up to date
        
        start_date = request.query_params.get('start_date')
        end_date = request.query_params.get('end_date')
        
        queryset = PortfolioNAV.objects.filter(portfolio=portfolio)
        
        if start_date:
            try:
                start_date = datetime.strptime(start_date, '%Y-%m-%d').date()
                queryset = queryset.filter(date__gte=start_date)
            except ValueError:
                return Response(
                    {'error': 'Invalid start_date format. Use YYYY-MM-DD'},
                    status=status.HTTP_400_BAD_REQUEST
                )
        
        if end_date:
            try:
                end_date = datetime.strptime(end_date, '%Y-%m-%d').date()
                queryset = queryset.filter(date__lte=end_date)
            except ValueError:
                return Response(
                    {'error': 'Invalid end_date format. Use YYYY-MM-DD'},
                    status=status.HTTP_400_BAD_REQUEST
                )
        
        nav_data = queryset.order_by('date').values('date', 'nav', 'daily_return')
        return Response(nav_data)

    @action(detail=True, methods=['get'])
    def holdings_returns(self, request, pk=None):
        """Get holdings returns for a portfolio"""
        portfolio = self.get_object()
        ensure_latest_data(portfolio)  # Ensure data is up to date
        
        start_date = request.query_params.get('start_date')
        end_date = request.query_params.get('end_date')
        ticker = request.query_params.get('ticker')
        
        queryset = HoldingReturn.objects.filter(portfolio=portfolio)
        
        if start_date:
            try:
                start_date = datetime.strptime(start_date, '%Y-%m-%d').date()
                queryset = queryset.filter(date__gte=start_date)
            except ValueError:
                return Response(
                    {'error': 'Invalid start_date format. Use YYYY-MM-DD'},
                    status=status.HTTP_400_BAD_REQUEST
                )
        
        if end_date:
            try:
                end_date = datetime.strptime(end_date, '%Y-%m-%d').date()
                queryset = queryset.filter(date__lte=end_date)
            except ValueError:
                return Response(
                    {'error': 'Invalid end_date format. Use YYYY-MM-DD'},
                    status=status.HTTP_400_BAD_REQUEST
                )
        
        if ticker:
            queryset = queryset.filter(holding__ticker=ticker.upper())
        
        # Group by ticker and return as a nested structure
        returns_by_ticker = {}
        for return_obj in queryset.order_by('holding__ticker', 'date'):
            ticker = return_obj.holding.ticker
            if ticker not in returns_by_ticker:
                returns_by_ticker[ticker] = []
            returns_by_ticker[ticker].append({
                'date': return_obj.date,
                'daily_return': return_obj.daily_return,
                'market_value': return_obj.market_value
            })
        
        return Response(returns_by_ticker)

    @action(detail=True, methods=['get'])
    def performance_analytics(self, request, pk=None):
        """Get performance analytics with benchmark comparison"""
        portfolio = self.get_object()
        ensure_latest_data(portfolio)  # Ensure data is up to date
        
        # Get parameters
        benchmark = request.query_params.get('benchmark', '^GSPC')  # Default to S&P 500
        range_param = request.query_params.get('range', '1Y')  # Default to 1 year
        
        # Calculate date range
        end_date = timezone.now().date()
        if range_param == '1M':
            start_date = end_date - timedelta(days=30)
        elif range_param == '3M':
            start_date = end_date - timedelta(days=90)
        elif range_param == '6M':
            start_date = end_date - timedelta(days=180)
        elif range_param == '1Y':
            start_date = end_date - timedelta(days=365)
        elif range_param == '3Y':
            start_date = end_date - timedelta(days=1095)
        elif range_param == '5Y':
            start_date = end_date - timedelta(days=1825)
        else:
            return Response(
                {'error': 'Invalid range parameter. Use 1M, 3M, 6M, 1Y, 3Y, or 5Y'},
                status=status.HTTP_400_BAD_REQUEST
            )
        
        # Get portfolio NAV data
        nav_data = PortfolioNAV.objects.filter(
            portfolio=portfolio,
            date__range=(start_date, end_date)
        ).order_by('date')
        
        # Get benchmark data
        benchmark_data = BenchmarkReturn.objects.filter(
            benchmark_name=benchmark,
            date__range=(start_date, end_date)
        ).order_by('date')
        
        # Calculate cumulative returns
        portfolio_returns = []
        benchmark_returns = []
        
        nav_value = 100  # Start at base 100
        for nav in nav_data:
            if nav.daily_return:
                nav_value *= (1 + nav.daily_return)
            portfolio_returns.append({
                'date': nav.date,
                'value': nav_value
            })
        
        bench_value = 100  # Start at base 100
        for bench in benchmark_data:
            if bench.daily_return:
                bench_value *= (1 + bench.daily_return)
            benchmark_returns.append({
                'date': bench.date,
                'value': bench_value
            })

        # Patch: Add metrics for frontend
        start_balance = float(portfolio.total_book_cost) if portfolio.total_book_cost is not None else 0.0
        end_balance = float(nav_data.last().total_value) if nav_data.exists() else 0.0
        metrics = {
            'start_balance': start_balance,
            'end_balance': end_balance,
            'annualized_return': None,
            'stddev': None,
            'best_year': None,
            'worst_year': None,
            'max_drawdown': None,
            'sharpe': None,
            'sortino': None,
        }

        # Defensive: Always include all keys expected by frontend
        # Rebase nav_norm and bench_norm to 100
        nav_norm = [float(nav.nav) for nav in nav_data] if nav_data.exists() else []
        bench_norm = [float(b['value']) for b in benchmark_returns] if benchmark_returns else []
        if nav_norm:
            base = nav_norm[0] if nav_norm[0] != 0 else 1.0
            nav_norm = [(v / base) * 100 for v in nav_norm]
        if bench_norm:
            base = bench_norm[0] if bench_norm[0] != 0 else 1.0
            bench_norm = [(v / base) * 100 for v in bench_norm]
        response = {
            'metrics': metrics,
            'dates': [str(nav.date) for nav in nav_data] if nav_data.exists() else [],
            'nav_norm': nav_norm,
            'bench_norm': bench_norm,
            'annual_returns': [],
            'portfolio_rolling_12m': [],
            'benchmark_rolling_12m': [],
            'portfolio_returns': portfolio_returns,
            'benchmark_returns': benchmark_returns
        }
        return Response(response)

    @action(detail=True, methods=['post'])
    def calculate_and_save_nav(self, request, pk=None):
        """Calculate and save NAV for the portfolio (for scheduled or manual use)"""
        portfolio = self.get_object()
        self.calculate_nav(portfolio)
        return Response({'status': 'NAV calculated and saved'})

    @action(detail=True, methods=['post'])
    def refresh_prices(self, request, pk=None):
        """
        Single entry point for updating market prices
        """
        portfolio = self.get_object()
        
        # Use a lock to prevent concurrent updates
        lock_key = f'price_update_lock_{portfolio.id}'
        if cache.get(lock_key):
            return Response({
                'status': 'pending',
                'message': 'Price update already in progress'
            })
        
        # Set lock for 5 minutes
        cache.set(lock_key, True, 300)
        
        try:
            # Clear any existing cached data
            cache.delete(f'portfolio_overview_{pk}')
            cache.delete(f'portfolio_asset_allocation_{pk}')
            
            # Queue the price update task
            task_queue.add_live_prices_task(portfolio.id)
            
            return Response({
                'status': 'success',
                'message': 'Price refresh queued'
            })
        except Exception as e:
            logger.error(f"Error queueing price refresh: {e}")
            # Release lock on error
            cache.delete(lock_key)
            return Response({
                'status': 'error',
                'message': str(e)
            }, status=status.HTTP_500_INTERNAL_SERVER_ERROR)

    @action(detail=True, methods=['get'])
    def holdings(self, request, pk=None):
        portfolio = self.get_object()
        holdings = Holding.objects.filter(portfolio=portfolio)
        serializer = HoldingSerializer(holdings, many=True)
        return Response(serializer.data)

    @action(detail=True, methods=['get'])
    def nav(self, request, pk=None):
        portfolio = self.get_object()
        date_str = request.query_params.get('date', timezone.now().date().isoformat())
        try:
            target_date = date.fromisoformat(date_str)
        except ValueError:
            return Response(
                {'error': 'Invalid date format. Use YYYY-MM-DD'},
                status=status.HTTP_400_BAD_REQUEST
            )

        holdings = Holding.objects.filter(portfolio=portfolio)
        market_values = update_market_prices(holdings, target_date)
        
        total_nav = sum(market_values.values())
        return Response({
            'date': target_date.isoformat(),
            'nav': str(total_nav),
            'holdings': market_values
        })

    @action(detail=True, methods=['get'])
    def risk_analytics(self, request, pk=None):
        import pandas as pd
        import numpy as np
        portfolio = self.get_object()
        benchmark_name = request.query_params.get('benchmark', '^GSPC')

        # Fetch NAV daily returns
        nav_qs = PortfolioNAV.objects.filter(portfolio=portfolio).order_by('date')
        nav_df = pd.DataFrame(list(nav_qs.values('date', 'daily_return')))
        nav_df = nav_df.dropna(subset=['daily_return'])
        if len(nav_df) < 2:
            return Response({"error": "Not enough NAV data to calculate risk metrics"}, status=400)

        # Fetch benchmark returns for same dates
        bench_qs = BenchmarkReturn.objects.filter(
            benchmark_name=benchmark_name,
            date__in=nav_df['date']
        ).order_by('date')
        bench_df = pd.DataFrame(list(bench_qs.values('date', 'daily_return')))

        # Align by date
        merged = pd.merge(nav_df, bench_df, on='date', how='inner', suffixes=('_portfolio', '_benchmark'))
        if len(merged) < 2:
            return Response({"error": "Not enough aligned data to calculate risk metrics"}, status=400)

        returns = merged['daily_return_portfolio'].astype(float)
        bench_returns = merged['daily_return_benchmark'].astype(float)

        # Metrics
        total_risk = np.std(returns) * np.sqrt(252)
        tracking_error = np.std(returns - bench_returns) * np.sqrt(252)
        value_at_risk = -np.percentile(returns, 5)
        ex_ante_vol = returns.rolling(window=21).std().iloc[-1] * np.sqrt(252)

        # Round
        result = {
            "total_risk": round(float(total_risk), 4),
            "tracking_error": round(float(tracking_error), 4),
            "value_at_risk": round(float(value_at_risk), 4),
            "ex_ante_volatility": round(float(ex_ante_vol), 4) if not np.isnan(ex_ante_vol) else None,
        }
        return Response(result)

class HoldingViewSet(viewsets.ModelViewSet):
    """
    ViewSet for Holding model operations.
    Provides CRUD operations and custom actions.
    """
    queryset = Holding.objects.all()
    serializer_class = HoldingSerializer

    def get_queryset(self):
        """Filter holdings by portfolio if specified"""
        portfolio_id = self.request.query_params.get('portfolio')
        qs = Holding.objects.all()
        if portfolio_id:
            qs = qs.filter(portfolio_id=portfolio_id)
        return qs.order_by('id')

    def perform_create(self, serializer):
        with transaction.atomic():
            # Get or create asset
            ticker = serializer.validated_data.get('ticker')
            if ticker:
                # Try to fetch real name from yfinance
                try:
                    info = yf.Ticker(ticker).info
                    asset_name = info.get('shortName') or info.get('longName') or ticker
                except Exception:
                    asset_name = ticker
                asset, created = Asset.objects.get_or_create(
                    ticker=ticker,
                    defaults={
                        'name': asset_name,
                        'asset_class': 'Equity',
                        'currency': 'USD',
                        'market': 'US'
                    }
                )
                # If asset already exists but name is default, update it
                if not created and asset.name == asset.ticker and asset_name != ticker:
                    asset.name = asset_name
                    asset.save(update_fields=['name'])
                serializer.validated_data['asset'] = asset
            # Save the holding
            holding = serializer.save()
            # Update market price if not cash
            price = get_or_fetch_market_price(
                holding.asset.ticker,
                timezone.now().date()
            )
            if price is not None:
                holding.current_price = price
                holding.market_value = price * holding.quantity
                holding.save()

    def perform_update(self, serializer):
        with transaction.atomic():
            # Get or create asset if ticker is provided
            ticker = serializer.validated_data.get('ticker')
            if ticker:
                # Try to fetch real name from yfinance
                try:
                    info = yf.Ticker(ticker).info
                    asset_name = info.get('shortName') or info.get('longName') or ticker
                except Exception:
                    asset_name = ticker
                asset, created = Asset.objects.get_or_create(
                    ticker=ticker,
                    defaults={
                        'name': asset_name,
                        'asset_class': 'Equity',
                        'currency': 'USD',
                        'market': 'US'
                    }
                )
                # If asset already exists but name is default, update it
                if not created and asset.name == asset.ticker and asset_name != ticker:
                    asset.name = asset_name
                    asset.save(update_fields=['name'])
                serializer.validated_data['asset'] = asset
            # Save the holding
            holding = serializer.save()
            # Update market price if not cash
            price = get_or_fetch_market_price(
                holding.asset.ticker,
                timezone.now().date()
            )
            if price is not None:
                holding.current_price = price
                holding.market_value = price * holding.quantity
                holding.save()

    @action(detail=True, methods=['get'])
    def market_value(self, request, pk=None):
        holding = self.get_object()
        date_str = request.query_params.get('date', timezone.now().date().isoformat())
        try:
            target_date = date.fromisoformat(date_str)
        except ValueError:
            return Response(
                {'error': 'Invalid date format. Use YYYY-MM-DD'},
                status=status.HTTP_400_BAD_REQUEST
            )

        if holding.asset_class == 'Cash':
            return Response({
                'date': target_date.isoformat(),
                'market_value': str(holding.book_cost)
            })

        if not holding.asset:
            return Response({
                'error': 'No asset associated with this holding'
            }, status=status.HTTP_400_BAD_REQUEST)

        price = get_or_fetch_market_price(holding.asset.ticker, target_date)
        if price is not None:
            market_value = price * holding.quantity
            return Response({
                'date': target_date.isoformat(),
                'market_value': str(market_value)
            })
        else:
            return Response({
                'error': 'Could not fetch market price'
            }, status=status.HTTP_404_NOT_FOUND)

class PortfolioNAVViewSet(viewsets.ModelViewSet):
    """
    ViewSet for PortfolioNAV model operations.
    """
    queryset = PortfolioNAV.objects.all()
    serializer_class = PortfolioNAVSerializer

# --- TradeHistoryViewSet ---
class TradeHistoryViewSet(viewsets.ModelViewSet):
    serializer_class = TradeHistorySerializer
    permission_classes = [AllowAny]
    filter_backends = [filters.OrderingFilter, filters.SearchFilter]
    ordering = ['-date']
    search_fields = ['trade_type', 'ticker', 'isin', 'asset_class', 'currency']

    def get_queryset(self):
        queryset = TradeHistory.objects.all()
        portfolio_id = self.request.query_params.get('portfolio_id')
        trade_type = self.request.query_params.get('trade_type')
        year = self.request.query_params.get('year')
        if portfolio_id:
            queryset = queryset.filter(portfolio_id=portfolio_id)
        if trade_type:
            queryset = queryset.filter(trade_type=trade_type)
        if year:
            queryset = queryset.filter(date__year=year)
        return queryset.order_by('-date')

class BenchmarksViewSet(viewsets.ViewSet):
    """API for listing benchmarks and their returns."""
    def list(self, request):
        # List available benchmarks (unique benchmark_name/display_name)
        qs = BenchmarkReturn.objects.values('benchmark_name', 'display_name').distinct()
        return Response(list(qs))

    @action(detail=True, methods=['get'], url_path='returns')
    def returns(self, request, pk=None):
        # pk is benchmark_name
        qs = BenchmarkReturn.objects.filter(benchmark_name=pk).order_by('date')
        serializer = BenchmarkReturnChartSerializer(qs, many=True)
        return Response(serializer.data)

@api_view(['GET'])
def get_fx_rate(request):
    """
    Get FX rate for a currency pair on a specific date.
    """
    base_currency = request.GET.get('base')
    target_currency = request.GET.get('target')
    date_str = request.GET.get('date')

    if not all([base_currency, target_currency, date_str]):
        return Response(
            {'error': 'base, target, and date parameters are required'}, 
            status=status.HTTP_400_BAD_REQUEST
        )

    try:
        # Parse date from DD/MM/YYYY format
        date_obj = datetime.strptime(date_str, '%d/%m/%Y').date()
    except ValueError:
        return Response(
            {'error': 'Invalid date format. Use DD/MM/YYYY'}, 
            status=status.HTTP_400_BAD_REQUEST
        )

    try:
        rate = get_or_fetch_fx_rate(base_currency, target_currency, date_obj)
        if rate is None:
            return Response(
                {'error': f'Could not get FX rate for {base_currency}/{target_currency} on {date_str}'}, 
                status=status.HTTP_404_NOT_FOUND
            )
        
        return Response({
            'base_currency': base_currency,
            'target_currency': target_currency,
            'rate': str(rate),
            'date': date_str
        })

    except Exception as e:
        logger.error(f"Error getting FX rate: {e}")
        return Response(
            {'error': 'Internal server error'}, 
            status=status.HTTP_500_INTERNAL_SERVER_ERROR
        )

@api_view(['GET'])
def get_portfolio_value(request, pk):
    """
    Get portfolio value in a specific currency.
    """
    target_currency = request.GET.get('currency')
    date_str = request.GET.get('date')

    if not target_currency:
        return Response(
            {'error': 'currency parameter is required'}, 
            status=status.HTTP_400_BAD_REQUEST
        )

    try:
        portfolio = Portfolio.objects.get(pk=pk)
    except Portfolio.DoesNotExist:
        return Response(
            {'error': 'Portfolio not found'}, 
            status=status.HTTP_404_NOT_FOUND
        )

    try:
        # Parse date from DD/MM/YYYY format if provided, otherwise use today
        date_obj = None
        if date_str:
            try:
                date_obj = datetime.strptime(date_str, '%d/%m/%Y').date()
            except ValueError:
                return Response(
                    {'error': 'Invalid date format. Use DD/MM/YYYY'}, 
                    status=status.HTTP_400_BAD_REQUEST
                )

        value = portfolio.get_nav_in_currency(target_currency, date_obj)
        if value is None:
            return Response(
                {'error': f'Could not calculate portfolio value in {target_currency}'}, 
                status=status.HTTP_404_NOT_FOUND
            )
        
        return Response({
            'portfolio_id': pk,
            'currency': target_currency,
            'value': str(value),
            'date': date_str or date.today().strftime('%d/%m/%Y')
        })

    except Exception as e:
        logger.error(f"Error getting portfolio value: {e}")
        return Response(
            {'error': 'Internal server error'}, 
            status=status.HTTP_500_INTERNAL_SERVER_ERROR
        )

class RiskAnalyticsView(APIView):
    permission_classes = [IsAuthenticated]

    def get(self, request, portfolio_id):
        # TODO: Replace with real portfolio_returns and benchmark_returns
        np.random.seed(0)
        portfolio_returns = np.random.normal(0, 0.01, 252)
        benchmark_returns = np.random.normal(0, 0.009, 252)

        vol = np.std(portfolio_returns) * np.sqrt(252)
        tracking_error = np.std(portfolio_returns - benchmark_returns) * np.sqrt(252)
        value_at_risk = -np.percentile(portfolio_returns, 5)
        forecast_risk = None  # Optional: implement if you have a model

        return Response({
            "total_risk": float(vol),
            "forecast_risk": forecast_risk,
            "tracking_error": float(tracking_error),
            "value_at_risk": float(value_at_risk),
        })
