import requests
import pandas as pd

GAINERS_URL = "https://stockanalysis.com/markets/gainers/"

def get_top_gainers(limit: int = 20) -> pd.DataFrame:
    """
    Fetch today's top stock gainers from StockAnalysis and return
    a tidy DataFrame with columns: Symbol, Company, % Change, Price, Volume, Market Cap.
    Set `limit` to control how many rows you want (default 20).
    """
    headers = {
        # A polite UA helps avoid being blocked by some servers:
        "User-Agent": "Mozilla/5.0 (X11; Linux x86_64) AppleWebKit/537.36 "
                      "(KHTML, like Gecko) Chrome/120.0.0.0 Safari/537.36"
    }
    r = requests.get(GAINERS_URL, headers=headers, timeout=30)
    r.raise_for_status()

    # The first table on the page is the one we want
    tables = pd.read_html(r.text)
    if not tables:
        raise RuntimeError("No tables found on the page.")

    df_raw = tables[0]

    # The site headers typically look like:
    # ['No.', 'Symbol', 'Company Name', '% Change', 'Stock Price', 'Volume', 'Market Cap']
    # Normalize and keep only the useful columns, then clean up formats.
    col_map = {
        'Company Name': 'Company',
        'Stock Price': 'Price',
        '% Change': '% Change',
        'Symbol': 'Symbol',
        'Volume': 'Volume',
        'Market Cap': 'Market Cap',
        'No.': 'No.'
    }
    df = df_raw.rename(columns=col_map)

    keep_cols = ['No.', 'Symbol', 'Company', '% Change', 'Price', 'Volume', 'Market Cap']
    df = df[[c for c in keep_cols if c in df.columns]].copy()

    # Tidy percentage/price fields if you want numeric types:
    def pct_to_float(s):
        return pd.to_numeric(s.astype(str).str.replace('%','', regex=False), errors='coerce')

    def money_to_float(s):
        return pd.to_numeric(s.astype(str).str.replace('$','', regex=False)
                                          .str.replace(',','', regex=False), errors='coerce')

    # Convert % Change, Price, Volume (comma separated), Market Cap (still string like '1.23B')
    if '% Change' in df:
        df['% Change'] = pct_to_float(df['% Change'])
    if 'Price' in df:
        df['Price'] = money_to_float(df['Price'])
    if 'Volume' in df:
        df['Volume'] = pd.to_numeric(df['Volume'].astype(str).str.replace(',','', regex=False), errors='coerce')

    # Keep only the top N rows
    if limit is not None:
        df = df.head(limit).reset_index(drop=True)

    return df

if __name__ == "__main__":
    top = get_top_gainers(limit=20)
    print(top)

    # If you just need the list of symbols (tickers):
    tickers = top['Symbol'].tolist()
    print("Tickers:", tickers)
