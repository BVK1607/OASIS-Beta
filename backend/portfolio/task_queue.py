from queue import Queue, PriorityQueue
import threading
import time
import logging
from typing import Optional, List, Tuple
from decimal import Decimal
from django.utils import timezone
from datetime import timedelta
from django.db import transaction, connections
import yfinance as yf
from django.core.cache import cache

logger = logging.getLogger(__name__)

class TaskQueue:
    def __init__(self):
        self._queue = []
        self._running = False
        self._lock = threading.Lock()
        
    def add_historical_data_task(self, portfolio_id: int, start_date=None, end_date=None):
        """Add a task to update historical market data"""
        with self._lock:
            self._queue.append({
                'type': 'historical_data',
                'portfolio_id': portfolio_id,
                'start_date': start_date,
                'end_date': end_date
            })
            if not self._running:
                self._start_processing()

    def add_live_prices_task(self, portfolio_id: int):
        """Add a task to refresh current prices for a portfolio's holdings"""
        with self._lock:
            self._queue.append({
                'type': 'live_prices',
                'portfolio_id': portfolio_id,
            })
            if not self._running:
                self._start_processing()
                
    def _process_historical_data_task(self, task):
        """Process a historical data update task"""
        from .models import Holding, Asset
        from .market_data import fetch_and_store_historical_prices
        
        portfolio_id = task['portfolio_id']
        start_date = task['start_date']
        end_date = task['end_date']
        
        # Get all unique tickers in the portfolio
        holdings = Holding.objects.filter(portfolio_id=portfolio_id)
        tickers = set(holdings.exclude(ticker='').values_list('ticker', flat=True))
        
        for ticker in tickers:
            try:
                # Get asset
                asset = Asset.objects.get(ticker=ticker)
                
                # Fetch historical prices
                success = fetch_and_store_historical_prices(ticker, start_date, end_date)
                if not success:
                    logger.error(f"Failed to fetch historical prices for {ticker}")
                    
            except Exception as e:
                logger.error(f"Error processing historical data for {ticker}: {e}")
                continue
                
    def _process_task(self, task):
        """Process a single task"""
        try:
            if task['type'] == 'historical_data':
                self._process_historical_data_task(task)
            elif task['type'] == 'live_prices':
                self._process_live_prices_task(task['portfolio_id'])
        except Exception as e:
            logger.error(f"Error processing task: {e}")
            
    def _start_processing(self):
        """Start processing tasks in the background (caller holds self._lock)"""
        self._running = True

        def process_loop():
            while True:
                with self._lock:
                    if not self._queue:
                        self._running = False
                        break
                    task = self._queue.pop(0)
                    
                self._process_task(task)
                
        thread = threading.Thread(target=process_loop)
        thread.daemon = True
        thread.start()

    def _process_live_queue(self):
        """Process live price updates with high priority"""
        while self._running:
            try:
                portfolio_id = self.live_queue.get()
                if portfolio_id is None:  # Poison pill
                    break
                    
                self._process_live_prices(portfolio_id)
                
            except Exception as e:
                logger.error(f"Error processing live price update: {str(e)}")
            finally:
                self.live_queue.task_done()
                # Clean up database connections
                connections.close_all()

    def _process_historical_queue(self):
        """Process historical data updates with lower priority"""
        while self._running:
            try:
                priority, (portfolio_id, start_date) = self.historical_queue.get()
                if portfolio_id is None:  # Poison pill
                    break
                    
                self._process_historical_data(portfolio_id, start_date)
                
            except Exception as e:
                logger.error(f"Error processing historical data: {str(e)}")
            finally:
                self.historical_queue.task_done()
                # Clean up database connections
                connections.close_all()

    def _process_historical_data(self, portfolio_id: int, start_date):
        """Process historical data for a portfolio from a specific date"""
        try:
            from .models import Portfolio, MarketPrice, PortfolioNAV, HoldingReturn
            from .market_data import bulk_ensure_market_prices
            
            portfolio = Portfolio.objects.get(id=portfolio_id)
            logger.info(f"Processing historical data for portfolio {portfolio_id}")

            # Get all holdings
            holdings = portfolio.holdings.all()
            
            if not holdings.exists():
                return
            
            # Get unique tickers and ensure we have market data
            tickers = holdings.exclude(asset_class='Cash').values_list('ticker', flat=True).distinct()
            if tickers:
                # Process in smaller chunks to avoid long transactions
                chunk_size = 30  # days
                current_start = start_date
                today = timezone.now().date()
                
                while current_start <= today:
                    current_end = min(current_start + timedelta(days=chunk_size), today)
                    bulk_ensure_market_prices(tickers, current_start, current_end)
                    current_start = current_end + timedelta(days=1)
            
            # Calculate daily NAV in chunks
            current_date = start_date
            today = timezone.now().date()
            
            while current_date <= today:
                if current_date.weekday() >= 5:  # Skip weekends
                    current_date += timedelta(days=1)
                    continue
                
                # Use a transaction for each day to avoid long-running transactions
                with transaction.atomic():
                    # Calculate total value for this date
                    total_value = Decimal('0')
                    holding_updates = []
                    
                    for holding in holdings:
                        try:
                            if holding.trade_date <= current_date:
                                if holding.asset_class == 'Cash':
                                    market_value = holding.book_cost
                                    total_value += market_value
                                    holding_updates.append({
                                        'holding': holding,
                                        'market_value': market_value
                                    })
                                else:
                                    market_price = MarketPrice.objects.filter(
                                        ticker=holding.ticker,
                                        date=current_date
                                    ).first()
                                    
                                    if market_price:
                                        market_value = market_price.price * holding.quantity
                                        total_value += market_value
                                        holding_updates.append({
                                            'holding': holding,
                                            'market_value': market_value
                                        })
                        except Exception as e:
                            logger.error(f"Error processing holding {holding.ticker} for date {current_date}: {e}")
                            continue
                    
                    if total_value > 0:
                        prev_nav = PortfolioNAV.objects.filter(
                            portfolio=portfolio,
                            date__lt=current_date
                        ).order_by('-date').first()
                        
                        daily_return = None
                        if prev_nav and prev_nav.total_value > 0:
                            daily_return = ((total_value / prev_nav.total_value) - 1) * 100
                        
                        nav = PortfolioNAV.objects.create(
                            portfolio=portfolio,
                            date=current_date,
                            total_value=total_value,
                            nav=total_value,
                            daily_return=daily_return
                        )
                        
                        holding_returns = []
                        for update in holding_updates:
                            holding = update['holding']
                            market_value = update['market_value']
                            
                            prev_return = HoldingReturn.objects.filter(
                                holding=holding,
                                date__lt=current_date
                            ).order_by('-date').first()
                            
                            daily_return = None
                            if prev_return and prev_return.market_value > 0:
                                daily_return = ((market_value / prev_return.market_value) - 1) * 100
                            
                            holding_returns.append(HoldingReturn(
                                portfolio=portfolio,
                                holding=holding,
                                date=current_date,
                                market_value=market_value,
                                daily_return=daily_return
                            ))
                        
                        if holding_returns:
                            HoldingReturn.objects.bulk_create(holding_returns)
                
                current_date += timedelta(days=1)
            
            # Rebase NAV in a separate transaction
            with transaction.atomic():
                first_nav = PortfolioNAV.objects.filter(portfolio=portfolio).order_by('date').first()
                if first_nav:
                    base_value = first_nav.total_value
                    for nav in PortfolioNAV.objects.filter(portfolio=portfolio):
                        nav.nav = Decimal('100.0') * (nav.total_value / base_value)
                        nav.save()
            
            logger.info(f"Completed historical data processing for portfolio {portfolio_id}")

        except Exception as e:
            logger.error(f"Error in historical data processing for portfolio {portfolio_id}: {str(e)}")

    def _process_live_prices(self, portfolio_id: int):
        """Process live price updates for an existing portfolio"""
        try:
            from .models import Portfolio
            from .market_data import get_market_price
            
            portfolio = Portfolio.objects.get(id=portfolio_id)
            logger.info(f"Processing live prices for portfolio {portfolio_id}")

            # Process each holding in its own transaction
            for holding in portfolio.holdings.all():
                if holding.asset_class == 'Cash':
                    continue

                try:
                    with transaction.atomic():
                        market_data = get_market_price(holding.ticker)
                        
                        if market_data['is_clean']:
                            holding.current_price = market_data
                            holding.market_value = market_data * holding.quantity
                            holding.last_price_update = market_data['timestamp']
                            holding.save()

                except Exception as e:
                    logger.error(f"Error updating live price for {holding.ticker}: {str(e)}")
                    continue

            logger.info(f"Completed live price updates for portfolio {portfolio_id}")

        except Exception as e:
            logger.error(f"Error in live price processing for portfolio {portfolio_id}: {str(e)}")

    def _process_live_prices_task(self, portfolio_id):
        """Process live price updates for a portfolio"""
        try:
            from .models import Portfolio, Holding
            
            portfolio = Portfolio.objects.get(id=portfolio_id)
            logger.info(f"Processing live prices for portfolio {portfolio_id}")
            
            # Get all non-cash holdings
            holdings = portfolio.holdings.exclude(asset_class='Cash')
            if not holdings.exists():
                return
            
            # Process holdings in smaller batches
            batch_size = 5
            for i in range(0, len(holdings), batch_size):
                batch = holdings[i:i + batch_size]
                
                # Update each holding in batch
                for holding in batch:
                    try:
                        if not holding.ticker:
                            continue
                            
                        stock = yf.Ticker(holding.ticker)
                        current_price = stock.info.get('regularMarketPrice')
                        
                        if current_price:
                            price = Decimal(str(current_price))
                            market_value = price * holding.quantity
                            
                            # Queryset update skips save() signals and keeps the write short
                            Holding.objects.filter(id=holding.id).update(
                                current_price=price,
                                market_value=market_value,
                            )
                    except Exception as e:
                        logger.error(f"Error updating price for {holding.ticker}: {e}")
                        continue
                
                # Small delay between batches
                time.sleep(0.5)
                
        except Exception as e:
            logger.error(f"Error processing live prices for portfolio {portfolio_id}: {e}")
        finally:
            # Always clear the lock when done
            cache.delete(f'price_update_lock_{portfolio_id}')

# Create global task queue instance
task_queue = TaskQueue() 