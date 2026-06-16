import requests
import pandas as pd
from bs4 import BeautifulSoup
from datetime import datetime
from pathlib import Path
import re

from playwright.sync_api import sync_playwright
SCRAPED_AT = datetime.now().strftime("%Y-%m-%d %H:%M:%S")


def clean_number(value):
    if value is None:
        return None

    text = str(value).replace(",", "").replace("−", "-")
    matches = re.findall(r"[-+]?\d*\.\d+|[-+]?\d+", text)

    if not matches:
        return None

    try:
        return float(matches[0])
    except:
        return None


def scrape_golden_grain():
    url = "https://www.cihedging.com/cih/api/index.cfm/origination/cashbids/98951"

    headers = {
        "User-Agent": "Mozilla/5.0",
        "Origin": "https://ggecorn.com",
        "Referer": "https://ggecorn.com/cash-bids%2Fcustomers",
        "Content-Type": "application/json"
    }

    response = requests.post(url, headers=headers, timeout=30)
    response.raise_for_status()

    try:
        html = response.json()
    except:
        html = response.text

    soup = BeautifulSoup(html, "html.parser")

    rows = []

    for tr in soup.find_all("tr"):
        cells = [td.get_text(" ", strip=True) for td in tr.find_all(["td", "th"])]

        if len(cells) < 5:
            continue

        if "Delivery" in cells[0]:
            continue

        delivery = cells[0]
        futures_month = cells[1] if len(cells) > 1 else ""

        numbers = [clean_number(c) for c in cells[2:]]
        numbers = [n for n in numbers if n is not None]

        futures_price = numbers[0] if len(numbers) > 0 else None
        bid_change = numbers[1] if len(numbers) > 1 else None
        basis_dollars = numbers[2] if len(numbers) > 2 else None
        cash_bid = numbers[3] if len(numbers) > 3 else None

        if cash_bid is None and futures_price is not None and basis_dollars is not None:
            cash_bid = futures_price + basis_dollars

        basis_cents = basis_dollars * 100 if basis_dollars is not None else None

        rows.append({
            "source": "Golden Grain Energy",
            "location": "Mason City, IA",
            "commodity": "Corn",
            "delivery_date": delivery,
            "futures_month": futures_month,
            "futures_price": futures_price,
            "basis_dollars": basis_dollars,
            "basis_cents": basis_cents,
            "cash_bid": cash_bid,
            "bid_change": bid_change,
            "as_of": "",
            "scraped_at": SCRAPED_AT
        })

    return rows


def scrape_landus():
    url = "https://www.landus.ag/api/cash-bids?location=5"

    headers = {
        "User-Agent": "Mozilla/5.0",
        "Referer": "https://www.landus.ag/businesses/grain/grain-bids"
    }

    response = requests.get(url, headers=headers, timeout=30)
    response.raise_for_status()

    data = response.json()
    as_of = data.get("asOfDateTime", "")

    rows = []

    for commodity_group in data.get("cashBids", []):
        commodity = commodity_group.get("commodity", "")

        for bid in commodity_group.get("bids", []):
            basis_dollars = bid.get("basisPrice", None)
            basis_cents = basis_dollars * 100 if basis_dollars is not None else None

            rows.append({
                "source": "Landus",
                "location": "Location 5",
                "commodity": commodity,
                "delivery_date": bid.get("deliveryDate", ""),
                "futures_month": bid.get("basisMonth", ""),
                "futures_price": "",
                "basis_dollars": basis_dollars,
                "basis_cents": basis_cents,
                "cash_bid": bid.get("currentBid", ""),
                "bid_change": bid.get("bidChange", ""),
                "as_of": as_of,
                "scraped_at": SCRAPED_AT
            })

    return rows


if __name__ == "__main__":
    all_rows = []

    print("Scraping Golden Grain...")
    try:
        all_rows.extend(scrape_golden_grain())
        print("Golden Grain done.")
    except Exception as e:
        print("Golden Grain error:", e)

    print("Scraping Landus...")
    try:
        all_rows.extend(scrape_landus())
        print("Landus done.")
    except Exception as e:
        print("Landus error:", e)

    df = pd.DataFrame(all_rows)

    output_path = Path(__file__).with_name("all_grain_bids.csv")
    df.to_csv(output_path, index=False)

    print(df)
    print(f"\nSaved to {output_path}")


def scrape_newcoop():
    url = "https://www.newcoop.com/cash-bids?location_name=Britt"

    with sync_playwright() as p:
        browser = p.chromium.launch(headless=True)
        page = browser.new_page()

        page.goto(url, wait_until="domcontentloaded", timeout=60000)
        page.wait_for_timeout(8000)

        html = page.content()
        browser.close()

    soup = BeautifulSoup(html, "html.parser")

    rows = []

    for tr in soup.find_all("tr"):
        cells = [td.get_text(" ", strip=True) for td in tr.find_all(["td", "th"])]

        if len(cells) < 5:
            continue

        text = " ".join(cells).lower()

        if "commodity" in text and "delivery" in text:
            continue

        if "corn" in text or "soybeans" in text or "bean" in text:
            rows.append({
                "source": "NEW Cooperative",
                "location": "Britt",
                "commodity": cells[0] if len(cells) > 0 else "",
                "delivery_date": cells[1] if len(cells) > 1 else "",
                "futures_month": "",
                "futures_price": cells[4] if len(cells) > 4 else "",
                "basis_dollars": "",
                "basis_cents": cells[3] if len(cells) > 3 else "",
                "cash_bid": cells[2] if len(cells) > 2 else "",
                "bid_change": cells[5] if len(cells) > 5 else "",
                "as_of": "",
                "scraped_at": SCRAPED_AT
            })

    return rows