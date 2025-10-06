# ============================================================================
# earnings_client.py - Earnings Date Client
# ============================================================================
from datetime import datetime, date
import yfinance as yf


class EarningsClient:
    """Handles fetching earnings dates from Yahoo Finance."""
    
    def get_earnings_date(self, ticker):
        """Get next earnings date for a ticker.
        
        Args:
            ticker: Stock ticker
            
        Returns:
            datetime object with earnings date, or None if not available
        """
        
        try:            
            stock = yf.Ticker(ticker)
            calendar = stock.calendar
            
            if not calendar or 'Earnings Date' not in calendar:
                print(f"No calendar data for {ticker}")
                return None
            
            earnings_dates = calendar['Earnings Date']
            
            if not earnings_dates or len(earnings_dates) == 0:
                print(f"No earnings date announced for {ticker}")
                return None
            
            earnings_date = earnings_dates[0]
            
            # Convert date to datetime if needed
            if isinstance(earnings_date, date) and not isinstance(earnings_date, datetime):
                earnings_date = datetime.combine(earnings_date, datetime.min.time())
            
            print(f"\nEarnings Date: {earnings_date.strftime('%Y-%m-%d')}")
            print(f"Earnings High: {calendar.get('Earnings High', 'N/A')}")
            print(f"Earnings Low: {calendar.get('Earnings Low', 'N/A')}")
            
            return earnings_date
            
        except Exception as e:
            print(f"Error fetching earnings: {e}")
            return None