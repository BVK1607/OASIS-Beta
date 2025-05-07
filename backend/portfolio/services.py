from django.utils import timezone
from django.db import transaction
from django.core.cache import cache
from decimal import Decimal
from datetime import date, timedelta
import logging
from typing import Optional, Dict, List
from .models import Portfolio, Holding, PortfolioNAV, GlobalMarketPrice, HoldingReturn
from .market_data import get_or_fetch_market_price, validate_market_data
from .task_queue import task_queue
import pandas as pd
import math

logger = logging.getLogger(__name__)

class PortfolioNAVService:
    """Service class for managing portfolio NAV calculations and updates"""
    
    @staticmethod
    def calculate_portfolio_nav(portfolio: Portfolio, target_date: date = None) -> Optional[Decimal]:
        """
        Calculate NAV for a portfolio on a specific date.
        This is the single source of truth for NAV calculations.
        """
        if target_date is None:
            target_date = timezone.now().date()
            
        # Check cache first
        cache_key = f'portfolio_nav_{portfolio.id}_{target_date}'
        cached_nav = cache.get(cache_key)
        if cached_nav is not None:
            return cached_nav
            
        try:
            # Get all holdings for this date
            holdings = portfolio.holdings.filter(purchase_date__lte=target_date)
            
            # Calculate total value
            total_value = Decimal('0')
            market_values = {}
            
            for holding in holdings:
                if holding.asset_class == 'Cash':
                    market_value = holding.book_cost
                else:
                    price = get_or_fetch_market_price(holding.ticker, target_date)
                    if price is None:
                        logger.warning(f"No price available for {holding.ticker} on {target_date}")
                        continue
                    market_value = price * holding.quantity
                
                market_values[holding.id] = market_value
                total_value += market_value
            
            # Get previous NAV for return calculation
            prev_nav = PortfolioNAV.objects.filter(
                portfolio=portfolio,
                date__lt=target_date
            ).order_by('-date').first()
            
            # Calculate daily return
            daily_return = None
            if prev_nav and prev_nav.total_value > 0:
                daily_return = ((total_value / prev_nav.total_value) - 1) * 100
            
            # Create or update NAV record
            nav, created = PortfolioNAV.objects.update_or_create(
                portfolio=portfolio,
                date=target_date,
                defaults={
                    'total_value': total_value,
                    'nav': total_value,  # For now, just use total value as NAV
                    'daily_return': daily_return,
                    'last_updated': timezone.now()
                }
            )
            
            # Cache the result
            cache.set(cache_key, total_value, 60 * 60)  # Cache for 1 hour
            
            return total_value
            
        except Exception as e:
            logger.error(f"Error calculating NAV for portfolio {portfolio.id} on {target_date}: {e}")
            return None
    
    @staticmethod
    def update_portfolio_nav(portfolio: Portfolio, force: bool = False) -> bool:
        """
        Update NAV for a portfolio, with optional force flag to bypass cache.
        Returns True if successful, False otherwise.
        """
        try:
            # Use a lock to prevent concurrent updates
            lock_key = f'nav_update_lock_{portfolio.id}'
            if cache.get(lock_key) and not force:
                logger.info(f"NAV update already in progress for portfolio {portfolio.id}")
                return False
                
            # Set lock for 5 minutes
            cache.set(lock_key, True, 300)
            
            # Queue the update task
            task_queue.add_live_prices_task(portfolio.id)
            
            return True
            
        except Exception as e:
            logger.error(f"Error queueing NAV update for portfolio {portfolio.id}: {e}")
            # Release lock on error
            cache.delete(lock_key)
            return False
    
    @staticmethod
    def recalculate_nav_history(portfolio: Portfolio, from_date: date = None) -> bool:
        """
        Recalculate NAV history from a specific date.
        Returns True if successful, False otherwise.
        """
        try:
            if from_date is None:
                # Get earliest holding date
                earliest_holding = portfolio.holdings.aggregate(
                    earliest_date=models.Min('purchase_date')
                )['earliest_date']
                if earliest_holding:
                    from_date = earliest_holding
                else:
                    from_date = timezone.now().date()
            
            # Get all dates from from_date to today
            dates = pd.date_range(start=from_date, end=timezone.now().date(), freq='D')
            
            for d in dates:
                nav = PortfolioNAVService.calculate_portfolio_nav(portfolio, d.date())
                if nav is None:
                    logger.warning(f"Failed to calculate NAV for {d.date()}")
                    continue
            
            return True
            
        except Exception as e:
            logger.error(f"Error recalculating NAV history for portfolio {portfolio.id}: {e}")
            return False
    
    @staticmethod
    def get_portfolio_performance(portfolio: Portfolio, start_date: date = None, end_date: date = None) -> Dict:
        """
        Get portfolio performance metrics for a date range.
        """
        if start_date is None:
            start_date = portfolio.creation_date
        if end_date is None:
            end_date = timezone.now().date()
            
        # Get NAV records for the date range
        nav_records = PortfolioNAV.objects.filter(
            portfolio=portfolio,
            date__range=[start_date, end_date]
        ).order_by('date')
        
        if not nav_records.exists():
            return {
                'total_return': 0,
                'annualized_return': 0,
                'volatility': 0,
                'sharpe_ratio': 0
            }
        
        # Calculate returns
        returns = [record.daily_return for record in nav_records if record.daily_return is not None]
        
        if not returns:
            return {
                'total_return': 0,
                'annualized_return': 0,
                'volatility': 0,
                'sharpe_ratio': 0
            }
        
        # Calculate metrics
        total_return = (nav_records.last().nav / nav_records.first().nav - 1) * 100
        
        # Annualized return
        days = (end_date - start_date).days
        annualized_return = ((1 + total_return/100) ** (365/days) - 1) * 100
        
        # Volatility (annualized)
        returns_series = pd.Series(returns)
        volatility = returns_series.std() * math.sqrt(252)
        
        # Sharpe ratio (assuming risk-free rate of 0)
        sharpe_ratio = annualized_return / volatility if volatility > 0 else 0
        
        return {
            'total_return': total_return,
            'annualized_return': annualized_return,
            'volatility': volatility,
            'sharpe_ratio': sharpe_ratio
        }

    @staticmethod
    def backfill_portfolio_time_series(portfolio):
        """
        Robustly backfill NAV and holding returns for a portfolio from the earliest purchase date to today.
        Ensures a PortfolioNAV record exists for every day, even if all values are zero or there are no holdings.
        Uses forward-fill logic for missing prices (up to 30 days back).
        Sets is_complete=False if any holding's price was missing/forward-filled/zeroed or if there are no holdings.
        After backfilling, rebase NAV so the first non-zero NAV is 100.
        """
        holdings = list(portfolio.holdings.all())
        if not holdings:
            # If no holdings at all, still create NAV records for a default range (e.g., 2020-01-01 to today)
            start_date = date(2020, 1, 1)
        else:
            start_date = min(h.purchase_date for h in holdings)
        end_date = timezone.now().date()
        all_dates = pd.date_range(start=start_date, end=end_date, freq='D')

        today = timezone.now().date()
        prev_nav = None
        prev_holding_values = {}

        def get_last_available_price(ticker, date):
            price = get_or_fetch_market_price(ticker, date)
            if price is not None:
                return price, False  # False = not forward-filled
            for offset in range(1, 31):
                prev_date = date - timedelta(days=offset)
                price = get_or_fetch_market_price(ticker, prev_date)
                if price is not None:
                    return price, True  # True = forward-filled
            logger.warning(f"No price found for {ticker} on {date} or previous 30 days.")
            return None, True

        for d in all_dates:
            if d.date() > today:
                continue  # Never process future dates
            total_value = Decimal('0')
            holding_values = {}
            is_complete = True
            # Only use holdings that exist on this day
            day_holdings = [h for h in holdings if h.purchase_date <= d.date()]
            if not day_holdings:
                is_complete = False
                logger.warning(f"No holdings for portfolio {portfolio.id} on {d.date()} - NAV set to 0 and marked incomplete.")
            for holding in day_holdings:
                if holding.asset_class == 'Cash':
                    value = holding.book_cost
                else:
                    price, was_forward_filled = get_last_available_price(holding.ticker, d.date())
                    if price is None:
                        value = Decimal('0')
                        is_complete = False
                        logger.warning(f"Incomplete NAV: {holding.ticker} missing price for {d.date()} (set to 0)")
                    else:
                        value = price * holding.quantity
                        if was_forward_filled:
                            is_complete = False
                            logger.warning(f"Incomplete NAV: {holding.ticker} forward-filled price for {d.date()}")
                holding_values[holding.id] = value
                total_value += value
            # Store NAV (always, even if total_value is 0)
            nav_obj, _ = PortfolioNAV.objects.update_or_create(
                portfolio=portfolio,
                date=d.date(),
                defaults={
                    'total_value': total_value,
                    'nav': total_value,
                    'last_updated': timezone.now(),
                    'is_complete': is_complete
                }
            )
            # Store holding returns (only if there are holdings)
            for holding in day_holdings:
                if holding.id not in holding_values:
                    continue
                prev_value = prev_holding_values.get(holding.id)
                curr_value = holding_values[holding.id]
                daily_return = None
                if prev_value is not None and prev_value != 0:
                    daily_return = (curr_value - prev_value) / prev_value
                HoldingReturn.objects.update_or_create(
                    portfolio=portfolio,
                    holding=holding,
                    date=d.date(),
                    defaults={
                        'market_value': curr_value,
                        'daily_return': daily_return
                    }
                )
            prev_nav = total_value
            prev_holding_values = holding_values.copy()

        # --- REBASE NAV TO 100 ---
        nav_records = PortfolioNAV.objects.filter(portfolio=portfolio).order_by('date')
        first_valid_nav = nav_records.filter(total_value__gt=0).first()
        if first_valid_nav:
            scaling_factor = Decimal('100.0') / first_valid_nav.total_value
            for nav in nav_records:
                nav.nav = (nav.total_value * scaling_factor).quantize(Decimal('0.01'))
                nav.save(update_fields=['nav'])
        return True 