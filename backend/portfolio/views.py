from django.shortcuts import render
from rest_framework import viewsets, status
from rest_framework.response import Response
from rest_framework.decorators import action
from django.shortcuts import get_object_or_404
from .models import Portfolio, Holding, NAVSnapshot, RebalanceSnapshot, TradeHistory, HoldingsReturnSnapshot, BenchmarkReturnSnapshot
from .serializers import PortfolioSerializer, HoldingSerializer, NAVSnapshotSerializer, RebalanceSnapshotSerializer, TradeHistorySerializer, HoldingsReturnSnapshotSerializer, BenchmarkReturnSnapshotSerializer
from .market_data import get_market_price
import pandas as pd
import yfinance as yf
from datetime import datetime, timedelta
from django.db import transaction
import io
import logging
from django.utils import timezone
from decimal import Decimal
from collections import defaultdict
from rest_framework import filters
from rest_framework.permissions import AllowAny

logger = logging.getLogger(__name__)

# Create your views here.

class PortfolioViewSet(viewsets.ModelViewSet):
    """
    ViewSet for Portfolio model operations.
    Provides CRUD operations and custom actions.
    """
    queryset = Portfolio.objects.all()
    serializer_class = PortfolioSerializer

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
                'quantity': request.data.get('quantity'),
                'book_cost': request.data.get('book_cost'),
                'currency': request.data.get('currency'),
                'trade_date': request.data.get('trade_date'),
            }
            
            logger.info(f"Processed holding data: {holding_data}")
            
            serializer = HoldingSerializer(data=holding_data)
            if serializer.is_valid():
                logger.info("Holding data is valid")
                holding = serializer.save()
                
                # Fetch current market price
                try:
                    current_price = get_market_price(holding.ticker)
                    if current_price:
                        holding.current_price = current_price
                        holding.market_value = current_price * holding.quantity
                        holding.last_price_update = datetime.now()
                        holding.save()
                        logger.info(f"Updated market price: {current_price}")
                except Exception as e:
                    logger.error(f"Error fetching market price: {str(e)}")
                
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

    @action(detail=True, methods=['post'])
    def upload_holdings(self, request, pk=None):
        portfolio = self.get_object()
        if 'file' not in request.FILES:
            return Response({'error': 'No file provided'}, status=status.HTTP_400_BAD_REQUEST)
        file = request.FILES['file']
        try:
            df = pd.read_csv(file)
            # Accept both legacy and modern headers
            legacy_cols = ['ISIN', 'Ticker', 'Quantity', 'Book Cost', 'Trade Date']
            modern_cols = ['asset_class', 'isin', 'ticker', 'quantity', 'book_cost', 'currency', 'trade_date']
            if all(col in df.columns for col in modern_cols):
                # Use modern headers
                required_columns = modern_cols
                use_modern = True
            elif all(col in df.columns for col in legacy_cols):
                # Use legacy headers
                required_columns = legacy_cols
                use_modern = False
            else:
                return Response(
                    {'error': f'CSV must contain either columns: {", ".join(modern_cols)} or {", ".join(legacy_cols)}'},
                    status=status.HTTP_400_BAD_REQUEST
                )
            holdings = []
            for _, row in df.iterrows():
                if use_modern:
                    # Try to parse date in multiple formats
                    trade_date = str(row['trade_date'])
                    for fmt in ('%d-%m-%Y', '%d/%m/%Y', '%Y-%m-%d'):
                        try:
                            trade_date_obj = datetime.strptime(trade_date, fmt).date()
                            break
                        except ValueError:
                            continue
                    else:
                        return Response({'error': f'Invalid date format for trade_date: {trade_date}'}, status=status.HTTP_400_BAD_REQUEST)
                    holding = Holding(
                        portfolio=portfolio,
                        asset_class=row['asset_class'],
                        isin=row['isin'],
                        ticker=row['ticker'],
                        quantity=row['quantity'],
                        book_cost=row['book_cost'],
                        currency=row['currency'],
                        trade_date=trade_date_obj
                    )
                else:
                    # Try to parse date in multiple formats
                    trade_date = str(row['Trade Date'])
                    for fmt in ('%d-%m-%Y', '%d/%m/%Y', '%Y-%m-%d'):
                        try:
                            trade_date_obj = datetime.strptime(trade_date, fmt).date()
                            break
                        except ValueError:
                            continue
                    else:
                        return Response({'error': f'Invalid date format for Trade Date: {trade_date}'}, status=status.HTTP_400_BAD_REQUEST)
                    holding = Holding(
                        portfolio=portfolio,
                        asset_class='Equity', # Default if not present
                        isin=row['ISIN'],
                        ticker=row['Ticker'],
                        quantity=row['Quantity'],
                        book_cost=row['Book Cost'],
                        currency='USD', # Default if not present
                        trade_date=trade_date_obj
                    )
                holdings.append(holding)
            with transaction.atomic():
                Holding.objects.bulk_create(holdings)
            return Response(
                {'message': f'Successfully uploaded {len(holdings)} holdings'},
                status=status.HTTP_201_CREATED
            )
        except Exception as e:
            logger.error(f"Error uploading holdings: {str(e)}")
            return Response({'error': str(e)}, status=status.HTTP_400_BAD_REQUEST)

    @action(detail=True, methods=['get'])
    def overview(self, request, pk=None):
        portfolio = self.get_object()
        holdings = portfolio.holdings.all()
        overview = []
        total_market_value = 0
        holding_data = []
        for holding in holdings:
            try:
                ticker = yf.Ticker(holding.ticker)
                price = ticker.history(period='1d')['Close'][-1]
                market_value = float(holding.quantity) * float(price)
                unrealized_return = (float(price) - float(holding.book_cost)) * float(holding.quantity)
                holding_data.append({
                    'isin': holding.isin,
                    'ticker': holding.ticker,
                    'quantity': holding.quantity,
                    'book_cost': holding.book_cost,
                    'trade_date': holding.trade_date,
                    'market_price': price,
                    'market_value': market_value,
                    'unrealized_return': unrealized_return
                })
                total_market_value += market_value
            except Exception:
                continue
        # Calculate weights
        for h in holding_data:
            h['weight'] = (h['market_value'] / total_market_value) * 100 if total_market_value > 0 else 0
        # Portfolio return summary
        total_book_cost = sum([float(h['book_cost']) * float(h['quantity']) for h in holding_data])
        total_unrealized = sum([h['unrealized_return'] for h in holding_data])
        portfolio_return = (total_unrealized / total_book_cost) * 100 if total_book_cost > 0 else 0
        # NAV time series
        nav_snapshots = NAVSnapshot.objects.filter(portfolio=portfolio).order_by('date')
        nav_series = [{'date': nav.date, 'nav': nav.nav} for nav in nav_snapshots]
        return Response({
            'holdings': holding_data,
            'total_market_value': total_market_value,
            'portfolio_return': portfolio_return,
            'nav_series': nav_series
        })

    @action(detail=True, methods=['post'])
    def update_prices(self, request, pk=None):
        """Update market prices for all holdings in the portfolio"""
        portfolio = self.get_object()
        portfolio.update_market_prices()
        return Response({'status': 'Market prices updated'})

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
        """Calculate asset allocation breakdown by asset class"""
        portfolio = self.get_object()
        holdings = portfolio.holdings.all()
        
        # Initialize allocation dictionary
        allocation = {
            'Equity': Decimal('0'),
            'Fixed Income': Decimal('0'),
            'Alternatives': Decimal('0'),
            'Cash': Decimal('0')
        }
        
        total_value = Decimal('0')
        
        # Calculate total value and sum by asset class
        for holding in holdings:
            if holding.market_value:
                allocation[holding.asset_class] += holding.market_value
                total_value += holding.market_value
        
        # Calculate percentages
        allocation_percentages = []
        for asset_class, value in allocation.items():
            percentage = (value / total_value * 100) if total_value > 0 else Decimal('0')
            allocation_percentages.append({
                'name': asset_class,
                'value': round(percentage, 2),
                'market_value': round(value, 2)
            })
        
        return Response({
            'total_value': round(total_value, 2),
            'allocation': allocation_percentages
        })

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
        holdings = portfolio.holdings.all()
        if not holdings:
            return
        # Find earliest trade date
        start_date = min([h.trade_date for h in holdings])
        end_date = datetime.today().date()
        date_range = pd.date_range(start=start_date, end=end_date, freq='D')
        nav_history = []
        for date in date_range:
            nav = 0
            for holding in holdings:
                if holding.trade_date > date.date():
                    continue
                try:
                    ticker = yf.Ticker(holding.ticker)
                    hist = ticker.history(start=holding.trade_date, end=date.date() + timedelta(days=1))
                    if not hist.empty:
                        price = hist['Close'][-1]
                        nav += float(holding.quantity) * float(price)
                except Exception:
                    continue
            nav_history.append({'date': date.date(), 'nav': nav})
        # Normalize NAV to 100
        if nav_history and nav_history[0]['nav'] > 0:
            base = nav_history[0]['nav']
            for n in nav_history:
                n['nav'] = (n['nav'] / base) * 100
        # Store NAV snapshots
        NAVSnapshot.objects.filter(portfolio=portfolio).delete()
        NAVSnapshot.objects.bulk_create([
            NAVSnapshot(portfolio=portfolio, date=n['date'], nav=n['nav']) for n in nav_history if n['nav'] > 0
        ])

    @action(detail=True, methods=['post'])
    def rebalance_preview(self, request, pk=None):
        portfolio = self.get_object()
        targets = request.data.get('targets', {})
        holdings = portfolio.holdings.all()
        # Debug logging for holdings
        debug_info = []
        missing_prices = False
        for h in holdings:
            debug_info.append(f"{h.ticker}: market_value={h.market_value}, current_price={h.current_price}, quantity={h.quantity}")
            if h.market_value is None or h.current_price is None or h.current_price == 0 or h.market_value == 0:
                missing_prices = True
        if missing_prices:
            logger.error(f"[REBALANCE] One or more holdings missing/zero price: {debug_info}")
            return Response({'error': 'One or more holdings have missing or zero price. Please check tickers and refresh prices.'}, status=status.HTTP_400_BAD_REQUEST)
        logger.info(f"[REBALANCE] Holdings before preview: {debug_info}")
        total_value = sum(h.market_value or 0 for h in holdings)
        suggestions = []
        for holding in holdings:
            current_value = holding.market_value or 0
            current_weight = (current_value / total_value * 100) if total_value > 0 else 0
            target_weight = float(targets.get(str(holding.id), current_weight))
            target_value = total_value * (Decimal(str(target_weight)) / Decimal('100'))
            price = holding.current_price or 1  # fallback to 1 to avoid div by zero
            target_units = target_value / price
            diff_units = target_units - (holding.quantity or 0)
            action = "BUY" if diff_units > 0 else ("SELL" if diff_units < 0 else "HOLD")
            suggestions.append({
                "ticker": holding.ticker,
                "action": action if abs(diff_units) > 1e-6 else "HOLD",
                "units": round(abs(diff_units), 2),
                "current_weight": round(current_weight, 2),
                "target_weight": round(target_weight, 2),
                "current_units": holding.quantity,
                "suggested_units": round(target_units, 2),
                "difference": round(float(target_weight) - float(current_weight), 2),
            })
        return Response({"suggestions": suggestions})

    @action(detail=True, methods=['post'])
    def rebalance_snapshot(self, request, pk=None):
        portfolio = self.get_object()
        data = request.data.copy()
        data['portfolio'] = portfolio.id
        serializer = RebalanceSnapshotSerializer(data=data)
        if serializer.is_valid():
            snapshot = serializer.save()
            return Response(RebalanceSnapshotSerializer(snapshot).data, status=status.HTTP_201_CREATED)
        else:
            return Response(serializer.errors, status=status.HTTP_400_BAD_REQUEST)

    @action(detail=True, methods=['get'])
    def rebalance_snapshots(self, request, pk=None):
        portfolio = self.get_object()
        snapshots = RebalanceSnapshot.objects.filter(portfolio=portfolio).order_by('-date_created')
        serializer = RebalanceSnapshotSerializer(snapshots, many=True)
        return Response(serializer.data)

    @action(detail=True, methods=['get'])
    def nav_history(self, request, pk=None):
        portfolio = self.get_object()
        nav_snapshots = NAVSnapshot.objects.filter(portfolio=portfolio).order_by('date')
        serializer = NAVSnapshotSerializer(nav_snapshots, many=True)
        return Response(serializer.data)

    @action(detail=True, methods=['get'])
    def holdings_returns(self, request, pk=None):
        portfolio = self.get_object()
        holding_returns = HoldingsReturnSnapshot.objects.filter(portfolio=portfolio).order_by('ticker', 'date')
        serializer = HoldingsReturnSnapshotSerializer(holding_returns, many=True)
        return Response(serializer.data)

class HoldingViewSet(viewsets.ModelViewSet):
    """
    ViewSet for Holding model operations.
    Provides CRUD operations and custom actions.
    """
    queryset = Holding.objects.all()
    serializer_class = HoldingSerializer
    
    def get_queryset(self):
        """
        Optionally restricts the returned holdings to a given portfolio,
        by filtering against a `portfolio` query parameter in the URL.
        """
        queryset = Holding.objects.all()
        portfolio_id = self.request.query_params.get('portfolio', None)
        if portfolio_id is not None:
            queryset = queryset.filter(portfolio_id=portfolio_id)
        return queryset

    def create(self, request, *args, **kwargs):
        """Override create to handle cash positions"""
        try:
            logger.info(f"Creating holding with data: {request.data}")
            
            # For cash positions, ensure correct data structure
            if request.data.get('asset_class') == 'Cash':
                logger.info("Processing cash position")
                data = {
                    'portfolio': request.data.get('portfolio'),
                    'asset_class': 'Cash',
                    'book_cost': request.data.get('book_cost'),
                    'currency': request.data.get('currency'),
                    'trade_date': request.data.get('trade_date'),
                    'isin': None,
                    'ticker': None,
                    'quantity': None
                }
                logger.info(f"Processed cash position data: {data}")
            else:
                data = request.data
            
            serializer = self.get_serializer(data=data)
            serializer.is_valid(raise_exception=True)
            logger.info(f"Serializer validation passed: {serializer.validated_data}")
            
            self.perform_create(serializer)
            headers = self.get_success_headers(serializer.data)
            
            logger.info(f"Successfully created holding: {serializer.data}")
            return Response(serializer.data, status=status.HTTP_201_CREATED, headers=headers)
            
        except Exception as e:
            logger.error(f"Error creating holding: {str(e)}", exc_info=True)
            return Response(
                {'error': f'Failed to create holding: {str(e)}'},
                status=status.HTTP_500_INTERNAL_SERVER_ERROR
            )

    def perform_create(self, serializer):
        """Set market value equal to book cost for cash positions and create TradeHistory record"""
        try:
            logger.info("Starting perform_create")
            holding = serializer.save()
            
            if holding.asset_class == 'Cash':
                logger.info(f"Setting up cash position with book cost: {holding.book_cost}")
                holding.market_value = holding.book_cost
                holding.current_price = holding.book_cost
                holding.last_price_update = timezone.now()
                holding.save()
                logger.info(f"Cash position updated with market value: {holding.market_value}")
            else:
                logger.info("Processing non-cash position")
                holding.update_market_price()
            
            # --- Create TradeHistory record ---
            try:
                # Calculate portfolio_weight_change
                total_market_value = holding.portfolio.total_market_value or 0
                if total_market_value > 0 and holding.quantity and holding.current_price:
                    weight_change = (holding.quantity * holding.current_price) / total_market_value * 100
                else:
                    weight_change = None
                TradeHistory.objects.create(
                    portfolio=holding.portfolio,
                    trade_type='Buy',
                    ticker=holding.ticker or '',
                    isin=holding.isin or '',
                    asset_class=holding.asset_class,
                    quantity=holding.quantity or 0,
                    price_at_trade=holding.current_price or holding.book_cost or 0,
                    book_cost=holding.book_cost or 0,
                    gross_amount=holding.book_cost or 0,
                    cash_flow_impact=-(abs((holding.quantity or 0) * (holding.current_price or holding.book_cost or 0))),
                    currency=holding.currency,
                    portfolio_weight_change=weight_change,
                    notes='Auto-generated on holding creation.'
                )
            except Exception as e:
                logger.error(f"Failed to create TradeHistory record: {str(e)}")
        except Exception as e:
            logger.error(f"Error in perform_create: {str(e)}", exc_info=True)
            raise

    def perform_update(self, serializer):
        """Update market value after updating a holding and create TradeHistory record"""
        holding = serializer.save()
        if holding.asset_class == 'Cash':
            holding.market_value = holding.book_cost
            holding.current_price = holding.book_cost
            holding.last_price_update = timezone.now()
            holding.save()
        else:
            holding.update_market_price()
        # --- Create TradeHistory record for adjustment ---
        try:
            total_market_value = holding.portfolio.total_market_value or 0
            if total_market_value > 0 and holding.quantity and holding.current_price:
                weight_change = (holding.quantity * holding.current_price) / total_market_value * 100
            else:
                weight_change = None
            TradeHistory.objects.create(
                portfolio=holding.portfolio,
                trade_type='Adjust',
                ticker=holding.ticker or '',
                isin=holding.isin or '',
                asset_class=holding.asset_class,
                quantity=holding.quantity or 0,
                price_at_trade=holding.current_price or holding.book_cost or 0,
                book_cost=holding.book_cost or 0,
                gross_amount=holding.book_cost or 0,
                cash_flow_impact=(holding.quantity or 0) * (holding.current_price or holding.book_cost or 0),
                currency=holding.currency,
                portfolio_weight_change=weight_change,
                notes='Auto-generated on holding update.'
            )
        except Exception as e:
            logger.error(f"Failed to create TradeHistory record on update: {str(e)}")
        return holding

    @action(detail=True, methods=['post'])
    def update_price(self, request, pk=None):
        """Update market price for a specific holding"""
        holding = self.get_object()
        if holding.asset_class == 'Cash':
            holding.market_value = holding.book_cost
            holding.current_price = holding.book_cost
            holding.last_price_update = timezone.now()
            holding.save()
            return Response({'status': 'Market price updated'})
        success = holding.update_market_price()
        if success:
            return Response({'status': 'Market price updated'})
        return Response(
            {'status': 'Failed to update market price'},
            status=status.HTTP_400_BAD_REQUEST
        )

    @action(detail=True, methods=['get'])
    def market_data(self, request, pk=None):
        """
        Retrieve market data for a specific holding.
        """
        holding = self.get_object()
        data = {
            'market_value': holding.market_value,
            'unrealized_gain_loss': holding.unrealized_gain_loss,
            'last_price_update': holding.last_price_update,
        }
        return Response(data)

class NAVSnapshotViewSet(viewsets.ModelViewSet):
    """
    ViewSet for NAVSnapshot model operations.
    """
    queryset = NAVSnapshot.objects.all()
    serializer_class = NAVSnapshotSerializer

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
