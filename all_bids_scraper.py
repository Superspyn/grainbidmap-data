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

    text = str(value).replace(",", "").replace("−", "-").replace("$", "")
    matches = re.findall(r"[-+]?\d*\.\d+|[-+]?\d+", text)

    if not matches:
        return None

    try:
        return float(matches[0])
    except:
        return None


def clean_price(value):
    num = clean_number(value)
    return f"{num:.2f}" if num is not None else ""


def normalize_commodity(value):
    text = str(value).strip().lower()

    if "soybean" in text or "bean" in text:
        return "Soybeans"

    if "corn" in text:
        return "Corn"

    return str(value).strip()


def normalize_delivery(value):
    text = str(value).strip()

    first_date = text.split("-")[0].strip()

    try:
        dt = datetime.strptime(first_date, "%m/%d/%Y")
        return dt.strftime("%b %Y")
    except:
        return text


def normalize_futures_month(value):
    text = str(value).strip().replace("’", "'")

    match = re.search(r"([A-Za-z]{3})'?(\d{2,4})", text)

    if match:
        month = match.group(1).title()
        year = match.group(2)

        if len(year) == 2:
            year = "20" + year

        return f"{month} {year}"

    return text


def estimate_futures_month(commodity, delivery):
    try:
        dt = datetime.strptime(delivery, "%b %Y")
    except:
        return ""

    month = dt.month
    year = dt.year
    commodity = commodity.lower()

    if "corn" in commodity:
        if month in [6, 7]:
            return f"Jul {year}"
        if month in [8, 9, 10, 11, 12]:
            return f"Dec {year}"
        if month in [1, 2, 3]:
            return f"Mar {year}"
        if month in [4, 5]:
            return f"Jul {year}"

    if "soybean" in commodity:
        if month in [6, 7]:
            return f"Jul {year}"
        if month in [8, 9, 10, 11]:
            return f"Nov {year}"
        if month in [12, 1]:
            return f"Jan {year}"
        if month in [2, 3]:
            return f"Mar {year}"
        if month in [4, 5]:
            return f"Jul {year}"

    return ""


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

        delivery = normalize_delivery(cells[0])
        futures_month = normalize_futures_month(cells[1]) if len(cells) > 1 else ""

        numbers = [clean_number(c) for c in cells[2:]]
        numbers = [n for n in numbers if n is not None]

        futures_price = numbers[0] if len(numbers) > 0 else None
        bid_change = numbers[1] if len(numbers) > 1 else None
        basis_dollars = numbers[2] if len(numbers) > 2 else None
        cash_bid = numbers[3] if len(numbers) > 3 else None

        if cash_bid is None and futures_price is not None and basis_dollars is not None:
            cash_bid = futures_price + basis_dollars

        basis_cents = round(basis_dollars * 100, 1) if basis_dollars is not None else ""

        rows.append({
            "source": "Golden Grain Energy",
            "location": "Mason City, IA",
            "commodity": "Corn",
            "delivery_date": delivery,
            "futures_month": futures_month,
            "futures_price": f"{futures_price:.2f}" if futures_price is not None else "",
            "basis_dollars": basis_dollars if basis_dollars is not None else "",
            "basis_cents": basis_cents,
            "cash_bid": f"{cash_bid:.2f}" if cash_bid is not None else "",
            "bid_change": bid_change if bid_change is not None else "",
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
        commodity = normalize_commodity(commodity_group.get("commodity", ""))

        for bid in commodity_group.get("bids", []):
            basis_dollars = bid.get("basisPrice", None)
            basis_cents = round(basis_dollars * 100, 1) if basis_dollars is not None else ""

            cash_bid = bid.get("currentBid", "")
            cash_bid = f"{cash_bid:.2f}" if isinstance(cash_bid, (int, float)) else clean_price(cash_bid)

            rows.append({
                "source": "Landus",
                "location": "Britt, IA",
                "commodity": commodity,
                "delivery_date": bid.get("deliveryDate", ""),
                "futures_month": bid.get("basisMonth", ""),
                "futures_price": "",
                "basis_dollars": basis_dollars if basis_dollars is not None else "",
                "basis_cents": basis_cents,
                "cash_bid": cash_bid,
                "bid_change": bid.get("bidChange", ""),
                "as_of": as_of,
                "scraped_at": SCRAPED_AT
            })

    return rows


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

        if "corn" in text or "soybean" in text or "bean" in text:
            commodity = normalize_commodity(cells[0])
            delivery = normalize_delivery(cells[1])
            cash_bid = clean_price(cells[2])
            basis_cents = clean_number(cells[3])
            basis_cents = round(basis_cents, 1) if basis_cents is not None else ""
            futures_month = estimate_futures_month(commodity, delivery)

            rows.append({
                "source": "NEW Cooperative",
                "location": "Britt, IA",
                "commodity": commodity,
                "delivery_date": delivery,
                "futures_month": futures_month,
                "futures_price": "",
                "basis_dollars": "",
                "basis_cents": basis_cents,
                "cash_bid": cash_bid,
                "bid_change": clean_number(cells[5]) if len(cells) > 5 else "",
                "as_of": "",
                "scraped_at": SCRAPED_AT
            })

    return rows


if __name__ == "__main__":
    all_rows = []

    print("Scraping Golden Grain...")
    try:
        golden_rows = scrape_golden_grain()
        print("Golden Grain rows found:", len(golden_rows))
        all_rows.extend(golden_rows)
    except Exception as e:
        print("Golden Grain error:", e)

    print("Scraping Landus...")
    try:
        landus_rows = scrape_landus()
        print("Landus rows found:", len(landus_rows))
        all_rows.extend(landus_rows)
    except Exception as e:
        print("Landus error:", e)

    print("Scraping NEW Coop...")
    try:
        newcoop_rows = scrape_newcoop()
        print("NEW Coop rows found:", len(newcoop_rows))
        all_rows.extend(newcoop_rows)
    except Exception as e:
        print("NEW Coop error:", e)

    df = pd.DataFrame(all_rows)

    output_path = Path(__file__).with_name("all_grain_bids.csv")
    df.to_csv(output_path, index=False)

    print(df)
    print("Rows saved:", len(df))
    print("CSV saved here:", output_path)
    print("Updated at:", SCRAPED_AT)
