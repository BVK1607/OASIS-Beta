from django.utils import timezone
import yfinance as yf
from decimal import Decimal
from typing import List
from .models import Holding
import pandas as pd

def update_market_prices(holdings: List[Holding]) -> List[Holding]:
    """
    Update market prices for the given holdings using yfinance.
    """
    for holding in holdings:
        try:
            # Get ticker data from yfinance
            ticker = yf.Ticker(holding.ticker)
            
            # Get the latest price
            hist = ticker.history(period='1d')
            if not hist.empty:
                current_price = hist['Close'].iloc[-1]
                
                # Update holding with new price data
                holding.current_price = Decimal(str(round(current_price, 2)))
                holding.market_value = holding.current_price * holding.quantity
                holding.last_price_update = timezone.now()
                holding.save()
            
        except Exception as e:
            print(f"Error updating price for {holding.ticker}: {str(e)}")
            continue
    
    return holdings

def get_market_price(ticker: str) -> Decimal:
    """
    Fetch current market price from yfinance
    """
    try:
        yf_ticker = yf.Ticker(ticker)
        hist = yf_ticker.history(period='1d')
        if not hist.empty:
            return Decimal(str(round(hist['Close'].iloc[-1], 2)))
    except Exception as e:
        print(f"Error fetching price for {ticker}: {str(e)}")
    
    return None

def get_historical_prices(ticker: str, start_date, end_date) -> pd.Series:
    """
    Fetch historical daily close prices for a ticker from Yahoo Finance.
    Returns a pandas Series indexed by date.
    """
    try:
        yf_ticker = yf.Ticker(ticker)
        hist = yf_ticker.history(start=start_date, end=end_date)
        if not hist.empty:
            return hist['Close']
    except Exception as e:
        print(f"Error fetching historical prices for {ticker}: {str(e)}")
    return pd.Series(dtype='float64')

# TODO: Implement real market data integration
# def get_market_price(isin: str, ticker: str) -> Decimal:
#     """
#     Fetch current market price from a market data provider
#     """
#     pass 