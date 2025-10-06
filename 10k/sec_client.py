# ============================================================================
# sec_client.py - SEC API Client (improved)
# ============================================================================

from __future__ import annotations

import time
import json
from datetime import datetime, timedelta, date
from typing import Optional, Dict, List

import requests


class SECClient:
    """Handles all SEC API interactions."""

    TICKERS_URL = "https://www.sec.gov/files/company_tickers.json"
    SUBMISSIONS_URL_TMPL = "https://data.sec.gov/submissions/CIK{cik}.json"
    ARCHIVES_IX_TPL = "https://www.sec.gov/ix?doc=/Archives/edgar/data/{cik_no_zeros}/{accession_no_dashes}/{primary_doc}"

    def __init__(
        self,
        include_amendments: bool = False,
        request_pause_seconds: float = 0.25,
        timeout: int = 30,
        session: Optional[requests.Session] = None,
    ):
        """
        Args:
            user_agent: A descriptive UA with contact info, e.g. "MyApp/1.0 (me@example.com)"
            include_amendments: If True, include 10-K/A as valid 10-Ks
            request_pause_seconds: Gentle delay between SEC requests
            timeout: HTTP timeout in seconds
        """

        self.user_agent = "YourName your.email@example.com"
        self.include_amendments = include_amendments
        self.request_pause_seconds = max(0.0, request_pause_seconds)
        self.timeout = timeout

        self._session = session or requests.Session()
        self._session.headers.update({
            "User-Agent": self.user_agent,
            "Accept": "application/json, text/plain, */*",
            "Accept-Encoding": "gzip, deflate",
            "Connection": "keep-alive",
        })

        self._ticker_to_cik: Optional[Dict[str, str]] = None  # e.g., {"NVDA": "0001045810"}

    # -------------------- Public API --------------------

    def get_cik(self, ticker: str) -> Optional[str]:
        """Get CIK (10 digits, zero-padded) for a ticker like 'NVDA'."""
        ticker = (ticker or "").strip().upper()
        if not ticker:
            return None

        if self._ticker_to_cik is None:
            self._ticker_to_cik = self._download_ticker_table()

        return self._ticker_to_cik.get(ticker)

    def get_latest_10k(self, ticker: str) -> Optional[dict]:
        """
        Get most recent 10-K (or 10-K/A if include_amendments=True) from submissions.
        Returns:
            {
              'title': '10-K filed YYYY-MM-DD',
              'filing_date': datetime,   # naive
              'link': 'https://www.sec.gov/ix?...',
              'summary': '<accession_number>'
            }
        or None.
        """
        cik = self.get_cik(ticker)
        if not cik:
            return None

        filings = self._get_recent_filings(cik)
        if not filings:
            return None

        # Walk recent in order; pick newest matching form
        latest_idx = self._find_latest_10k_index(filings)
        if latest_idx is None:
            return None

        filing_date_str = filings.get("filingDate", [])[latest_idx]
        accession = filings.get("accessionNumber", [])[latest_idx]
        primary_doc = filings.get("primaryDocument", [])[latest_idx]

        try:
            filing_dt = datetime.strptime(filing_date_str, "%Y-%m-%d")
        except Exception:
            filing_dt = None

        link = self._build_ix_url(cik, accession, primary_doc)

        return {
            "title": f"10-K filed {filing_date_str}",
            "filing_date": filing_dt,
            "link": link,
            "summary": accession,
        }

    def get_10k_by_date(self, ticker: str, target_date: datetime, days_range: int = 30) -> Optional[dict]:
        """
        Get 10-K filing close to target_date (± days_range).
        """
        filings = self.get_10k_urls(ticker, years=3)
        if not filings:
            return None

        for filing in filings:
            try:
                filing_date = datetime.strptime(filing["filing_date"], "%Y-%m-%d")
            except Exception:
                continue

            if abs((filing_date - target_date).days) <= days_range:
                return {
                    "title": f"10-K filed {filing['filing_date']}",
                    "filing_date": filing_date,
                    "link": filing["url"],
                    "summary": filing["accession_number"],
                }

        return None

    def get_10k_urls(self, ticker: str, years: int = 3) -> List[dict]:
        """
        Get 10-K (and optionally 10-K/A) URLs for last N years, newest first.
        Returns list of dicts:
            {'filing_date': 'YYYY-MM-DD', 'accession_number': 'XXXX-XX-XXXXX', 'url': '<ix viewer url>'}
        """
        cik = self.get_cik(ticker)
        if not cik:
            return []

        filings = self._get_recent_filings(cik)
        if not filings:
            return []

        date_threshold = datetime.now() - timedelta(days=365 * max(1, years))
        out: List[dict] = []

        forms = filings.get("form", [])
        filing_dates = filings.get("filingDate", [])
        accessions = filings.get("accessionNumber", [])
        primary_docs = filings.get("primaryDocument", [])

        cik_no_zeros = str(int(cik))  # strip leading zeros for archive path

        for i in range(min(len(forms), len(filing_dates), len(accessions), len(primary_docs))):
            form = (forms[i] or "").upper().strip()
            if not self._is_10k_form(form):
                continue

            fdate_str = filing_dates[i]
            try:
                fdate = datetime.strptime(fdate_str, "%Y-%m-%d")
            except Exception:
                continue
            if fdate < date_threshold:
                continue

            accession = accessions[i]
            primary_doc = primary_docs[i]
            url = self._build_ix_url(cik, accession, primary_doc, cik_no_zeros=cik_no_zeros)

            out.append({
                "filing_date": fdate_str,
                "accession_number": accession,
                "url": url,
            })

        # newest first
        return sorted(out, key=lambda x: x["filing_date"], reverse=True)

    def get_last_10k_date(self, ticker: str) -> Optional[date]:
        """
        Convenience: return the latest 10-K filing date (as datetime.date) or None.
        """
        item = self.get_latest_10k(ticker)
        if not item or not item["filing_date"]:
            return None
        return item["filing_date"].date()

    # -------------------- Internals --------------------

    def _download_ticker_table(self) -> Dict[str, str]:
        """
        Build {TICKER: CIK(10-digit string)} from SEC ticker file.
        """
        data = self._get_json(self.TICKERS_URL)
        mapping: Dict[str, str] = {}
        if isinstance(data, dict):
            for _, entry in data.items():
                try:
                    t = str(entry.get("ticker", "")).upper()
                    cik_int = int(entry.get("cik_str"))
                    if t and cik_int:
                        mapping[t] = f"{cik_int:010d}"
                except Exception:
                    continue
        return mapping

    def _get_recent_filings(self, cik: str) -> Optional[dict]:
        """
        Return filings['recent'] dict or None.
        """
        url = self.SUBMISSIONS_URL_TMPL.format(cik=cik)
        data = self._get_json(url)
        if not data:
            return None
        filings = data.get("filings", {}).get("recent")
        return filings or None

    def _find_latest_10k_index(self, filings_recent: dict) -> Optional[int]:
        """
        From the 'recent' filings table, return the index of the newest 10-K (or 10-K/A).
        """
        forms = filings_recent.get("form", [])
        filing_dates = filings_recent.get("filingDate", [])
        if not forms or not filing_dates:
            return None

        latest_idx = None
        latest_dt = None

        for i, form in enumerate(forms):
            if not self._is_10k_form((form or "").upper().strip()):
                continue
            fdate_str = filing_dates[i]
            try:
                fdt = datetime.strptime(fdate_str, "%Y-%m-%d")
            except Exception:
                continue
            if latest_dt is None or fdt > latest_dt:
                latest_dt = fdt
                latest_idx = i

        return latest_idx

    def _is_10k_form(self, form_value: str) -> bool:
        if self.include_amendments:
            return form_value in {"10-K", "10-K/A"}
        return form_value == "10-K"

    def _build_ix_url(
        self,
        cik: str,
        accession: str,
        primary_doc: str,
        cik_no_zeros: Optional[str] = None,
    ) -> str:
        """
        Build the inline XBRL viewer URL for a filing.
        """
        if not cik_no_zeros:
            cik_no_zeros = str(int(cik))
        accession_no_dashes = (accession or "").replace("-", "")
        return self.ARCHIVES_IX_TPL.format(
            cik_no_zeros=cik_no_zeros,
            accession_no_dashes=accession_no_dashes,
            primary_doc=primary_doc,
        )

    def _get_json(self, url: str) -> Optional[dict]:
        """
        GET JSON with timeout & polite pause. Returns dict or None on error.
        """
        try:
            resp = self._session.get(url, timeout=self.timeout)
            time.sleep(self.request_pause_seconds)  # be nice to SEC
            resp.raise_for_status()
            return resp.json()
        except (requests.RequestException, json.JSONDecodeError):
            return None


# -------------------- Quick test --------------------
if __name__ == "__main__":
    client = SECClient(user_agent="YourApp/1.0 (someone@gmail.com)", include_amendments=False)
    print("NVDA last 10-K date:", client.get_last_10k_date("AAPL"))
    print("NVDA latest 10-K object:", client.get_latest_10k("AAPL"))
    print("NVDA recent 10-K URLs:", client.get_10k_urls("AAPL", years=3))
