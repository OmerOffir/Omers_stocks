# Imports
from sec_client import SECClient
from earnings_client import EarningsClient
from analyzer import FinancialAnalyzer
from monitor import TenKMonitor


# Run
if __name__ == "__main__":
    TICKER = "AAPL"
    sec_client = SECClient()
    earnings_client = EarningsClient()
    analyzer = FinancialAnalyzer()
    
    monitor = TenKMonitor(sec_client, earnings_client, analyzer, None)
    
    monitor.start_monitoring(TICKER, True)