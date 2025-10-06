# ============================================================================
# analyzer.py - OpenAI Financial Analyzer
# ============================================================================

from openai import OpenAI
from dotenv import load_dotenv
import os
import requests


class FinancialAnalyzer:
    """Analyzes 10-K filings using OpenAI."""
    
    def __init__(self):
        load_dotenv(override=True)
        google_api_key = os.getenv('GOOGLE_API_KEY')
        self.client = OpenAI(api_key=google_api_key, base_url="https://generativelanguage.googleapis.com/v1beta/openai/")
    
    def get_current_price(self, ticker):
        """Get current stock price from Yahoo Finance."""
        try:
            url = f"https://query1.finance.yahoo.com/v8/finance/chart/{ticker}?interval=1d&range=1d"
            headers = {'User-Agent': 'Mozilla/5.0'}
            
            response = requests.get(url, headers=headers)
            data = response.json()
            
            price = data['chart']['result'][0]['meta']['regularMarketPrice']
            print(f"Fetched current price for {ticker}: ${price}")
            return price
            
        except Exception as e:
            print(f"Could not fetch price: {e}")
            return None
    
    def extract_financials(self, url):
        """Extract financial data from 10-K URL."""
        response = self.client.chat.completions.create(
            model="gpt-4o",
            messages=[
                {"role": "system", "content": "You are a financial analyst expert."},
                {"role": "user", "content": f"""
Access this 10-K and extract financial data for BOTH current and prior year:

Required Data:
1. Revenue (both years)
2. Net Income (both years)
3. EPS (both years)
4. Total Assets, Liabilities, Equity (both years)
5. Current Assets/Liabilities (both years)
6. Cash (both years)
7. Operating Cash Flow (both years)
8. Total Debt (both years)
9. Book Value Per Share (both years)
10. Shares Outstanding (both years)

Present in clear table format.

URL: {url}
"""}
            ],
            temperature=0
        )
        return response.choices[0].message.content
    
    def analyze_investment(self, financial_data, ticker, filing_date, is_latest):
        """Analyze investment opportunity."""
        
        if is_latest:
            # Get current price in Python
            current_price = self.get_current_price(ticker)
            
            if current_price:
                price_text = f"${current_price:.2f}"
            else:
                price_text = "UNAVAILABLE"
            
            prompt = f"""
Analyze {ticker} for filing {filing_date}.

**CURRENT STOCK PRICE (already fetched): {price_text}**
DO NOT fetch any URLs or APIs. Use the price above for all calculations.

Financial Data:
{financial_data}

**Calculate these ratios using price {price_text}:**
1. P/E = {price_text} / EPS
2. P/B = {price_text} / Book Value Per Share
3. PEG, Debt/Equity, ROE, ROA, margins, growth rates

VALUE SCORE (Graham/Buffett criteria): X/7
GROWTH SCORE (Lynch/CAN SLIM): X/5
RED FLAGS: 2-3 concerns
RECOMMENDATION: 🟢 BUY / 🟡 HOLD / 🔴 SELL + reason

Max 250 words.
"""
        else:
            # Historical - no current price
            prompt = f"""
Historical analysis for {ticker} ({filing_date}):

{financial_data}

KEY RATIOS: Debt/Equity, Current Ratio, ROE, ROA, margins, growth
FINANCIAL HEALTH: 3-4 key observations

Max 200 words.
"""
        
        response = self.client.chat.completions.create(
            model="gpt-4o",
            messages=[
                {"role": "system", "content": "You are an expert value/growth investor."},
                {"role": "user", "content": prompt}
            ],
            temperature=0
        )
        return response.choices[0].message.content
    """Analyzes 10-K filings using OpenAI."""
    
    def __init__(self):
        load_dotenv(override=True)
        google_api_key = os.getenv('GOOGLE_API_KEY')
        self.client = OpenAI(api_key=google_api_key, base_url="https://generativelanguage.googleapis.com/v1beta/openai/")
    
    def extract_financials(self, url):
        """Extract financial data from 10-K URL.
        
        Args:
            url: 10-K filing URL
            
        Returns:
            String with extracted financial data
        """
        response = self.client.chat.completions.create(
            model="gemini-2.5-flash",
            messages=[
                {
                    "role": "system",
                    "content": "You are a financial analyst expert."
                },
                {
                    "role": "user",
                    "content": f"""
Access this 10-K and extract financial data for BOTH current and prior year:

Required Data:
1. Revenue (both years)
2. Net Income (both years)
3. EPS (both years)
4. Total Assets, Liabilities, Equity (both years)
5. Current Assets/Liabilities (both years)
6. Cash (both years)
7. Operating Cash Flow (both years)
8. Total Debt (both years)
9. Book Value Per Share (both years)
10. Shares Outstanding (both years)

Present in clear table format.

URL: {url}
"""
                }
            ],
            temperature=0
        )
        
        return response.choices[0].message.content
    
    def analyze_investment(self, financial_data, ticker, filing_date, is_latest):
        """Analyze investment opportunity."""
        
        if is_latest:
            # Get current price from Yahoo Finance IN PYTHON
            current_price = self.get_current_price(ticker)
            
            if not current_price:
                current_price_text = "UNAVAILABLE"
            else:
                current_price_text = f"${current_price:.2f}"
            
            # DO NOT ask GPT to fetch - give it the price
            prompt = f"""
    Analyze {ticker} filing {filing_date}.

    **CURRENT STOCK PRICE: {current_price_text}**
    DO NOT fetch this price. It's already provided above. Use it for calculations.

    Financial Data:
    {financial_data}

    Calculate using price {current_price_text}:
    - P/E = price / EPS
    - P/B = price / Book Value Per Share
    - PEG, Debt/Equity, Current Ratio, ROE, ROA
    - Growth rates

    VALUE SCORE (Graham/Buffett): X/7
    GROWTH SCORE (Lynch/CAN SLIM): X/5
    RED FLAGS: List 2-3
    RECOMMENDATION: 🟢 BUY / 🟡 HOLD / 🔴 SELL + reason

    Max 250 words.
    """
            """Analyze investment opportunity.
            
            Args:
                financial_data: Financial data string
                ticker: Stock ticker
                filing_date: Filing date string
                is_latest: True if this is the most recent filing
                
            Returns:
                String with investment analysis
            """
            if is_latest:
                # Latest filing - include current price analysis
                prompt = f"""
    **STEP 1: Get Current Stock Price**
    Fetch from: https://query1.finance.yahoo.com/v8/finance/chart/{ticker}?interval=1d&range=1d

    Extract the price from: response['chart']['result'][0]['meta']['regularMarketPrice']

    **STEP 2: Calculate Ratios**
    - P/E = regularMarketPrice / EPS
    - P/B = regularMarketPrice / Book Value Per Share
    - PEG = P/E / EPS Growth Rate

    **STEP 3: Investment Analysis for {ticker} ({filing_date})**

    Financial Data:
    {financial_data}

    **Analysis Sections:**

    1. **KEY RATIOS (vs Prior Year)**
    - P/E, P/B, PEG (using current price)
    - Debt-to-Equity, Current Ratio
    - ROE, ROA, Profit Margin
    - EPS Growth %, Revenue Growth %

    2. **VALUE STOCK (Graham/Buffett)**
    Check: Current Ratio > 2, Debt/Equity < 0.5, P/E < 15, P/B < 1.5, ROE > 15%
    **VALUE SCORE: X/7**

    3. **GROWTH STOCK (Lynch/CAN SLIM)**
    Check: PEG < 1, EPS Growth > 25%, Revenue Growth > 25%, Strong ROE
    **GROWTH SCORE: X/5**

    4. **RED FLAGS**
    List 2-3 concerns

    5. **RECOMMENDATION**
    🟢 BUY / 🟡 HOLD / 🔴 SELL
    One sentence why.

    **Max 250 words.**
    """
            else:
                # Historical filing - simpler analysis
                prompt = f"""
    Historical Analysis for {ticker} ({filing_date}):

    Financial Data:
    {financial_data}

    **Analysis:**

    1. **KEY RATIOS (vs Prior Year)**
    - Debt-to-Equity, Current Ratio
    - ROE, ROA, Profit Margin
    - EPS Growth %, Revenue Growth %

    2. **FINANCIAL HEALTH**
    - Balance sheet strength
    - Profitability trends
    - Growth trajectory

    3. **KEY HIGHLIGHTS**
    3-4 important observations

    **Max 200 words.**
    """
            
            response = self.client.chat.completions.create(
                model="gemini-2.5-flash",
                messages=[
                    {
                        "role": "system",
                        "content": "You are an expert value/growth investor."
                    },
                    {
                        "role": "user",
                        "content": prompt
                    }
                ],
                temperature=0
            )
            
            return response.choices[0].message.content
        
if __name__ == "__main__":
    analyzer = FinancialAnalyzer()
    test_url = "https://www.sec.gov/Archives/edgar/data/320193/000032019324000010/aapl-20230930.htm"
    financials = analyzer.extract_financials(test_url)
    print("Extracted Financials:")
    print(financials)
    
    analysis = analyzer.analyze_investment(financials, "AAPL", "2023-09-30", is_latest=True)
    print("\nInvestment Analysis:")
    print(analysis)