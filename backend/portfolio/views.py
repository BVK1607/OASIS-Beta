from django.shortcuts import render
from rest_framework import viewsets, status
from rest_framework.response import Response
from rest_framework.decorators import action
from django.shortcuts import get_object_or_404
from .models import Portfolio, Holding, PortfolioNAV, RebalancePlan, TradeHistory, HoldingReturn, BenchmarkReturn
from .serializers import PortfolioSerializer, HoldingSerializer, PortfolioNAVSerializer, RebalancePlanSerializer, TradeHistorySerializer, HoldingReturnSerializer, BenchmarkReturnSerializer, BenchmarkListSerializer, BenchmarkReturnChartSerializer
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
import numpy as np
import math

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
                    for fmt in ('%d/%m/%Y', '%d-%m-%Y', '%Y-%m-%d'):
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
                    for fmt in ('%d/%m/%Y', '%d-%m-%Y', '%Y-%m-%d'):
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
                # Create TradeHistory records for each holding (ensure date is set)
                for holding in holdings:
                    TradeHistory.objects.create(
                        portfolio=holding.portfolio,
                        trade_type='Buy',
                        ticker=holding.ticker or '',
                        isin=holding.isin or '',
                        asset_class=holding.asset_class,
                        quantity=holding.quantity or 0,
                        price_at_trade=holding.book_cost or 0,
                        book_cost=holding.book_cost or 0,
                        gross_amount=holding.book_cost or 0,
                        cash_flow_impact=-(abs((holding.quantity or 0) * (holding.book_cost or 0))),
                        currency=holding.currency,
                        portfolio_weight_change=None,
                        notes='Auto-generated from CSV upload.'
                    )
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
        missing_prices = False
        for holding in holdings:
            if holding.current_price is None or holding.market_value is None:
                missing_prices = True
            market_value = float(holding.market_value) if holding.market_value is not None else 0
            unrealized_return = (float(holding.current_price) - float(holding.book_cost)) * float(holding.quantity) if holding.current_price is not None else 0
            holding_data.append({
                'isin': holding.isin,
                'ticker': holding.ticker,
                'quantity': holding.quantity,
                'book_cost': holding.book_cost,
                'trade_date': holding.trade_date,
                'market_price': holding.current_price,
                'market_value': market_value,
                'unrealized_return': unrealized_return
            })
            total_market_value += market_value
        # Calculate weights
        for h in holding_data:
            h['weight'] = (h['market_value'] / total_market_value) * 100 if total_market_value > 0 else 0
        # Portfolio return summary
        total_book_cost = sum([float(h['book_cost']) * float(h['quantity']) for h in holding_data])
        total_unrealized = sum([h['unrealized_return'] for h in holding_data])
        portfolio_return = (total_unrealized / total_book_cost) * 100 if total_book_cost > 0 else 0
        # NAV time series
        nav_snapshots = PortfolioNAV.objects.filter(portfolio=portfolio).order_by('date')
        nav_series = [{'date': nav.date, 'nav': nav.nav} for nav in nav_snapshots]
        response = {
            'holdings': holding_data,
            'total_market_value': total_market_value,
            'portfolio_return': portfolio_return,
            'nav_series': nav_series
        }
        if missing_prices:
            response['warning'] = 'Some holdings are missing market prices. Please refresh prices.'
        return Response(response)

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
        PortfolioNAV.objects.filter(portfolio=portfolio).delete()
        PortfolioNAV.objects.bulk_create([
            PortfolioNAV(portfolio=portfolio, date=n['date'], nav=n['nav']) for n in nav_history if n['nav'] > 0
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
        """Get NAV history for a portfolio with optional date range filtering"""
        portfolio = self.get_object()
        start_date = request.query_params.get('start_date')
        end_date = request.query_params.get('end_date')
        
        queryset = PortfolioNAV.objects.filter(portfolio=portfolio)
        
        if start_date:
            try:
                start_date = datetime.strptime(start_date, '%Y-%m-%d').date()
                queryset = queryset.filter(date__gte=start_date)
            except ValueError:
                return Response({'error': 'Invalid start_date format. Use YYYY-MM-DD'}, 
                              status=status.HTTP_400_BAD_REQUEST)
        
        if end_date:
            try:
                end_date = datetime.strptime(end_date, '%Y-%m-%d').date()
                queryset = queryset.filter(date__lte=end_date)
            except ValueError:
                return Response({'error': 'Invalid end_date format. Use YYYY-MM-DD'}, 
                              status=status.HTTP_400_BAD_REQUEST)
        
        serializer = PortfolioNAVSerializer(queryset.order_by('date'), many=True)
        return Response(serializer.data)

    @action(detail=True, methods=['get'])
    def holdings_returns(self, request, pk=None):
        """Get holdings returns for a portfolio with optional date range and ticker filtering"""
        portfolio = self.get_object()
        start_date = request.query_params.get('start_date')
        end_date = request.query_params.get('end_date')
        ticker = request.query_params.get('ticker')
        
        queryset = HoldingReturn.objects.filter(portfolio=portfolio)
        
        if start_date:
            try:
                start_date = datetime.strptime(start_date, '%Y-%m-%d').date()
                queryset = queryset.filter(date__gte=start_date)
            except ValueError:
                return Response({'error': 'Invalid start_date format. Use YYYY-MM-DD'}, 
                              status=status.HTTP_400_BAD_REQUEST)
        
        if end_date:
            try:
                end_date = datetime.strptime(end_date, '%Y-%m-%d').date()
                queryset = queryset.filter(date__lte=end_date)
            except ValueError:
                return Response({'error': 'Invalid end_date format. Use YYYY-MM-DD'}, 
                              status=status.HTTP_400_BAD_REQUEST)
        
        if ticker:
            queryset = queryset.filter(ticker=ticker.upper())
        
        # Group by ticker and return as a nested structure
        returns_by_ticker = {}
        for snapshot in queryset.order_by('ticker', 'date'):
            if snapshot.ticker not in returns_by_ticker:
                returns_by_ticker[snapshot.ticker] = []
            returns_by_ticker[snapshot.ticker].append({
                'date': snapshot.date,
                'daily_return': snapshot.daily_return
            })
        
        return Response(returns_by_ticker)

    @action(detail=True, methods=['get'], url_path='performance_analytics')
    def performance_analytics(self, request, pk=None):
        """
        Return portfolio and benchmark performance analytics for the selected time range and benchmark.
        """
        def clean_list(l):
            return [None if (x is None or (isinstance(x, float) and (math.isnan(x) or math.isinf(x)))) else x for x in l]
        portfolio = self.get_object()
        benchmark = request.query_params.get('benchmark', '^GSPC')
        range_map = {
            '1M': 21, '3M': 63, '6M': 126, 'YTD': None, '1Y': 252, '3Y': 756, '5Y': 1260, 'SI': None
        }
        range_param = request.query_params.get('range', '5Y')
        nav_qs = PortfolioNAV.objects.filter(portfolio=portfolio).order_by('date')
        bench_qs = BenchmarkReturn.objects.filter(benchmark_name=benchmark).order_by('date')
        if not nav_qs.exists() or not bench_qs.exists():
            return Response({'error': 'Insufficient data for portfolio or benchmark.'}, status=400)
        nav_df = pd.DataFrame(list(nav_qs.values('date', 'nav')))
        bench_df = pd.DataFrame(list(bench_qs.values('date', 'return_index')))
        # Convert Decimal columns to float for pandas compatibility
        nav_df['nav'] = nav_df['nav'].astype(float)
        bench_df['return_index'] = bench_df['return_index'].astype(float)
        if nav_df.empty or bench_df.empty:
            return Response({'error': 'Insufficient data for portfolio or benchmark.'}, status=400)
        nav_df['date'] = pd.to_datetime(nav_df['date'])
        bench_df['date'] = pd.to_datetime(bench_df['date'])
        df = pd.merge(nav_df, bench_df, on='date', how='outer').sort_values('date')
        df['nav'] = df['nav'].ffill()
        df['return_index'] = df['return_index'].ffill()
        # Normalize both to 100 at the first available date
        df['nav_norm'] = df['nav'] / df['nav'].iloc[0] * 100
        df['bench_norm'] = df['return_index'] / df['return_index'].iloc[0] * 100
        # Annual returns (calendar years)
        df['year'] = df['date'].dt.year
        annual_returns = []
        for year, group in df.groupby('year'):
            port_start = group['nav_norm'].iloc[0]
            port_end = group['nav_norm'].iloc[-1]
            bench_start = group['bench_norm'].iloc[0]
            bench_end = group['bench_norm'].iloc[-1]
            port_ret = (port_end / port_start - 1) * 100 if port_start else None
            bench_ret = (bench_end / bench_start - 1) * 100 if bench_start else None
            annual_returns.append({
                'year': int(year),
                'portfolio_return': round(port_ret, 2) if port_ret is not None and not math.isnan(port_ret) and not math.isinf(port_ret) else None,
                'benchmark_return': round(bench_ret, 2) if bench_ret is not None and not math.isnan(bench_ret) and not math.isinf(bench_ret) else None,
            })
        # Start and end balances
        start_balance = df['nav'].iloc[0] if not math.isnan(df['nav'].iloc[0]) and not math.isinf(df['nav'].iloc[0]) else None
        end_balance = df['nav'].iloc[-1] if not math.isnan(df['nav'].iloc[-1]) and not math.isinf(df['nav'].iloc[-1]) else None
        # Filter by range
        if range_param == 'YTD':
            start = pd.Timestamp(datetime(datetime.now().year, 1, 1))
            df = df[df['date'] >= start]
        elif range_param != 'SI' and range_param in range_map:
            days = range_map[range_param]
            if days is not None:
                df = df.iloc[-days:]
        # Cumulative returns
        df['portfolio_cum_return'] = df['nav_norm'] / 100 - 1
        df['benchmark_cum_return'] = df['bench_norm'] / 100 - 1
        # Rolling 12M returns (trailing)
        df['portfolio_rolling_12m'] = df['nav_norm'].pct_change(periods=252).fillna(0)
        df['benchmark_rolling_12m'] = df['bench_norm'].pct_change(periods=252).fillna(0)
        # Annualized return (CAGR)
        n_years = (df['date'].iloc[-1] - df['date'].iloc[0]).days / 365.25
        total_return = df['nav_norm'].iloc[-1] / df['nav_norm'].iloc[0] - 1
        bench_total_return = df['bench_norm'].iloc[-1] / df['bench_norm'].iloc[0] - 1
        cagr = (df['nav_norm'].iloc[-1] / df['nav_norm'].iloc[0]) ** (1 / n_years) - 1 if n_years > 0 else 0
        bench_cagr = (df['bench_norm'].iloc[-1] / df['bench_norm'].iloc[0]) ** (1 / n_years) - 1 if n_years > 0 else 0
        rel_cagr = cagr - bench_cagr
        # Standard deviation (annualized)
        stddev = df['portfolio_cum_return'].pct_change().std() * np.sqrt(252)
        # Best/worst year
        df['year'] = df['date'].dt.year
        yearly = df.groupby('year').agg({'portfolio_cum_return': lambda x: (x.iloc[-1] + 1) / (x.iloc[0] + 1) - 1})
        best_year = yearly['portfolio_cum_return'].max() if not yearly.empty else None
        worst_year = yearly['portfolio_cum_return'].min() if not yearly.empty else None
        # Max drawdown
        roll_max = df['nav_norm'].cummax()
        drawdown = df['nav_norm'] / roll_max - 1
        max_drawdown = drawdown.min()
        # Sharpe/Sortino (risk-free rate assumed 0)
        daily_returns = df['portfolio_cum_return'].pct_change().dropna()
        sharpe = (daily_returns.mean() / daily_returns.std()) * np.sqrt(252) if daily_returns.std() > 0 else None
        sortino = (daily_returns.mean() / daily_returns[daily_returns < 0].std()) * np.sqrt(252) if (daily_returns < 0).std() > 0 else None
        # TWR (Time Weighted Return)
        twr = df['portfolio_cum_return'].iloc[-1]
        # MWR (Money Weighted Return) - not implemented, placeholder
        mwr = None
        # Replace NaN and inf with None for JSON serialization
        df = df.replace([np.inf, -np.inf], np.nan)
        df = df.where(pd.notnull(df), None)
        # Prepare response
        result = {
            'dates': [d.strftime('%d/%m/%Y') for d in df['date']],
            'nav_norm': clean_list(df['nav_norm'].round(6).tolist()),
            'bench_norm': clean_list(df['bench_norm'].round(6).tolist()),
            'annual_returns': annual_returns,
            'portfolio_cum': clean_list(df['portfolio_cum_return'].round(6).tolist()),
            'benchmark_cum': clean_list(df['benchmark_cum_return'].round(6).tolist()),
            'portfolio_rolling_12m': clean_list(df['portfolio_rolling_12m'].round(6).tolist()),
            'benchmark_rolling_12m': clean_list(df['benchmark_rolling_12m'].round(6).tolist()),
            'metrics': {
                'start_balance': round(start_balance, 2) if start_balance is not None else None,
                'end_balance': round(end_balance, 2) if end_balance is not None else None,
                'total_return': round(total_return * 100, 2) if total_return is not None and not np.isnan(total_return) else None,
                'annualized_return': round(cagr * 100, 2) if cagr is not None and not np.isnan(cagr) else None,
                'benchmark_return': round(bench_cagr * 100, 2) if bench_cagr is not None and not np.isnan(bench_cagr) else None,
                'benchmark_relative': round(rel_cagr * 100, 2) if rel_cagr is not None and not np.isnan(rel_cagr) else None,
                'stddev': round(stddev * 100, 2) if stddev is not None and not np.isnan(stddev) else None,
                'best_year': round(best_year * 100, 2) if best_year is not None and not np.isnan(best_year) else None,
                'worst_year': round(worst_year * 100, 2) if worst_year is not None and not np.isnan(worst_year) else None,
                'max_drawdown': round(max_drawdown * 100, 2) if max_drawdown is not None and not np.isnan(max_drawdown) else None,
                'sharpe': round(sharpe, 2) if sharpe is not None and not np.isnan(sharpe) else None,
                'sortino': round(sortino, 2) if sortino is not None and not np.isnan(sortino) else None,
                'twr': round(twr * 100, 2) if twr is not None and not np.isnan(twr) else None,
                'mwr': mwr,
            },
        }
        return Response(result)

    @action(detail=True, methods=['post'])
    def calculate_and_save_nav(self, request, pk=None):
        """Calculate and save NAV for the portfolio (for scheduled or manual use)"""
        portfolio = self.get_object()
        self.calculate_nav(portfolio)
        return Response({'status': 'NAV calculated and saved'})

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
