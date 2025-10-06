# ============================================================================
# monitor.py - Main Monitoring Logic (final version)
# ============================================================================

import time
from datetime import datetime

class TenKMonitor:
    """Monitors for new 10-K filings and analyzes them."""
    
    def __init__(self, sec_client, earnings_client, analyzer, simulate_date=None):
        self.sec = sec_client
        self.earnings = earnings_client
        self.analyzer = analyzer
        self.simulate_date = simulate_date
    
    def get_current_time(self):
        """Get current time (or simulated time)."""
        return self.simulate_date if self.simulate_date else datetime.now()
    
    def _within_10_days(self, filing_dt: datetime) -> bool:
        today = self.get_current_time().date()
        fdate = filing_dt.date()
        return (today - fdate).days <= 10

    def start_monitoring(self, ticker, auto_analyze):
        """Start monitoring for new 10-K filings."""
        print(f"\n{'='*80}")
        print(f"MONITORING {ticker} FOR NEW 10-K FILINGS")
        print(f"{'='*80}\n")
        
        # SIMULATION MODE
        if self.simulate_date:
            print(f"SIMULATION MODE: {self.simulate_date.strftime('%Y-%m-%d')}")
            filing = self.sec.get_10k_by_date(ticker, self.simulate_date)
            if filing:
                print(f"Found 10-K filed {filing['filing_date'].strftime('%Y-%m-%d')}")
                print(f"URL: {filing['link']}\n")
                if auto_analyze:
                    if self._within_10_days(filing['filing_date']):
                        self._analyze_filing(ticker, filing['link'], filing['filing_date'])
                    else:
                        print("Skipping: filing is more than 10 days old.")
            else:
                print("No 10-K found near simulated date.")
            print("\nSimulation complete.")
            return
        
        # REAL-TIME MODE (single check and exit if too old)
        latest = self.sec.get_latest_10k(ticker)
        if latest:
            filing_date = latest['filing_date']
            print(f"Latest 10-K filed {filing_date.strftime('%Y-%m-%d')}")
            print(f"URL: {latest['link']}\n")
            if auto_analyze:
                if self._within_10_days(filing_date):
                    return self._analyze_filing(ticker, latest['link'], filing_date)
                else:
                    print("Skipping: filing is more than 10 days old.")
        else:
            print("No recent 10-K found.")

    def _analyze_filing(self, ticker, url, filing_date):
        print("Extracting financials...")
        try:
            financials = self.analyzer.extract_financials(url)
            analysis = self.analyzer.analyze_investment(
                financials,
                ticker,
                filing_date.strftime('%Y-%m-%d'),
                is_latest=True
            )
            filename = f"{ticker}_{filing_date.strftime('%Y%m%d')}_analysis.txt"
            with open(filename, "w") as f:
                f.write(f"10-K Analysis for {ticker}\n")
                f.write(f"Filing Date: {filing_date.strftime('%Y-%m-%d')}\n")
                f.write(f"URL: {url}\n\n")
                f.write(analysis)
            return analysis, filename, url  # <--- return useful stuff
        except Exception as e:
            return None, None, None
