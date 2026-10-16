import csv
import json
import os
import re
import time
from urllib.parse import quote_plus
import xml.etree.ElementTree as ET
from bs4 import BeautifulSoup
from google import genai
from google.genai import types
from playwright.sync_api import sync_playwright
import requests

# ==========================================
# KONFIGURÁCIA (Načítanie z GitHub Secrets)
# ==========================================
GEMINI_API_KEY = os.environ.get("GEMINI_API_KEY", "")
TELEGRAM_BOT_TOKEN = os.environ.get("TELEGRAM_BOT_TOKEN", "")
TELEGRAM_CHAT_ID = os.environ.get("TELEGRAM_CHAT_ID", "")

DISCOUNT_THRESHOLD = 0.60
MIN_PRICE_EUR = 40
CSV_OUTPUT = "vyhodne_inzeraty.csv"
SEEN_FILE = "seen_ads.txt"

if not GEMINI_API_KEY:
  raise ValueError("Chýba GEMINI_API_KEY! Nastav ho v GitHub Secrets.")

client = genai.Client(api_key=GEMINI_API_KEY)


def load_seen_ads() -> set:
  if not os.path.exists(SEEN_FILE):
    return set()
  with open(SEEN_FILE, "r", encoding="utf-8") as f:
    return set(line.strip() for line in f if line.strip())


def save_seen_ads(seen_set: set):
  recent_ads = list(seen_set)[-1000:]
  with open(SEEN_FILE, "w", encoding="utf-8") as f:
    for ad_id in recent_ads:
      f.write(f"{ad_id}\n")


# ==========================================
# NEPRIESTRELNÉ SŤAHOVANIE BAZOŠU (3 METÓDY)
# ==========================================
def parse_rss_xml(xml_text: str) -> list:
  ads = []
  try:
    root = ET.fromstring(xml_text)
    for item in root.findall(".//item"):
      title_raw = item.findtext("title") or ""
      link = item.findtext("link") or ""
      desc_html = item.findtext("description") or ""

      match_id = re.search(r"/inzerat/(\d+)/", link)
      ad_id = match_id.group(1) if match_id else link

      # V RSS je názov v tvare: "Názov inzerátu: 150" alebo "Názov - 150"
      price_match = re.search(r"[:\-]\s*([\d\s]+)$", title_raw.strip())
      if not price_match:
        continue

      price_str = price_match.group(1).replace(" ", "").strip()
      if not price_str.isdigit():
        continue

      price = float(price_str)
      if price < MIN_PRICE_EUR:
        continue

      clean_title = title_raw[: price_match.start()].strip()
      clean_desc = BeautifulSoup(desc_html, "html.parser").get_text(
          " ", strip=True
      )

      ads.append({
          "id": ad_id,
          "title": clean_title,
          "description": clean_desc,
          "price": price,
          "url": link,
      })
  except Exception as e:
    print(f"Chyba pri spracovaní RSS XML: {e}")
  return ads


def parse_html_flexible(html_text: str) -> list:
  ads = []
  soup = BeautifulSoup(html_text, "html.parser")
  seen_in_batch = set()

  # Hľadáme akékoľvek odkazy na inzeráty (nezávisle od CSS tried Bazošu)
  for a_tag in soup.select('a[href*="/inzerat/"]'):
    href = a_tag.get("href", "")
    match_id = re.search(r"/inzerat/(\d+)/", href)
    if not match_id:
      continue

    ad_id = match_id.group(1)
    if ad_id in seen_in_batch:
      continue

    title = a_tag.get_text(strip=True)
    if len(title) < 3:
      continue  # Preskočíme obrázkové odkazy bez textu

    seen_in_batch.add(ad_id)
    link = (
        href if href.startswith("http") else f"https://hudba.bazos.sk{href}"
    )

    container = a_tag.find_parent("div", class_="inzeraty") or a_tag.find_parent(
        "table"
    ) or a_tag.parent.parent
    if not container:
      continue

    desc_el = container.select_one("div.popis")
    desc = (
        desc_el.get_text(" ", strip=True)
        if desc_el
        else container.get_text(" ", strip=True)
    )

    container_text = container.get_text(" ", strip=True)
    price_match = re.search(r"(\d[\d\s]*)\s*€", container_text)
    if not price_match:
      continue

    price_str = price_match.group(1).replace(" ", "").strip()
    if not price_str.isdigit():
      continue

    price = float(price_str)
    if price < MIN_PRICE_EUR:
      continue

    ads.append({
        "id": ad_id,
        "title": title,
        "description": desc,
        "price": price,
        "url": link,
    })

  return ads


def fetch_bazos_ads() -> list:
  headers = {
      "User-Agent": (
          "Mozilla/5.0 (Windows NT 10.0; Win64; x64) AppleWebKit/537.36 (KHTML,"
          " like Gecko) Chrome/122.0.0.0 Safari/537.36"
      ),
      "Accept-Language": "sk-SK,sk;q=0.9,cs;q=0.8,en;q=0.7",
  }

  # 1. Pokus: Priamy RSS kanál Bazošu + Proxy zálohy pre RSS
  rss_urls = [
      "https://www.bazos.sk/rss.php?rub=hu",
      f"https://api.allorigins.win/raw?url={quote_plus('https://www.bazos.sk/rss.php?rub=hu')}",
      f"https://corsproxy.io/?{quote_plus('https://www.bazos.sk/rss.php?rub=hu')}",
  ]

  for url in rss_urls:
    try:
      print(f"Skúšam stiahnuť RSS: {url[:45]}...")
      r = requests.get(url, headers=headers, timeout=15)
      if r.status_code == 200 and "<item>" in r.text:
        ads = parse_rss_xml(r.text)
        if ads:
          print(f" -> Úspech cez RSS! Nájdených {len(ads)} inzerátov s cenou.")
          return ads
    except Exception as e:
      print(f" -> RSS pokus zlyhal: {e}")

  # 2. Pokus: HTML stránka priamo + cez Proxy
  html_urls = [
      "https://hudba.bazos.sk/",
      f"https://api.allorigins.win/raw?url={quote_plus('https://hudba.bazos.sk/')}",
      f"https://corsproxy.io/?{quote_plus('https://hudba.bazos.sk/')}",
  ]

  for url in html_urls:
    try:
      print(f"Skúšam stiahnuť HTML: {url[:45]}...")
      r = requests.get(url, headers=headers, timeout=15)
      if r.status_code == 200 and "/inzerat/" in r.text:
        ads = parse_html_flexible(r.text)
        if ads:
          print(f" -> Úspech cez HTML! Nájdených {len(ads)} inzerátov s cenou.")
          return ads
    except Exception as e:
      print(f" -> HTML pokus zlyhal: {e}")

  return []


# ==========================================
# AI ANALÝZA A POROVNANIE CIEN
# ==========================================
def parse_ad_with_ai(title: str, description: str) -> dict:
  prompt = f"""
    Analyzuj tento inzerát z Bazošu v kategórii hudobné nástroje.
    Nadpis: {title}
    Popis: {description}

    Vráť JSON s týmito poľami:
    - "is_valid_sale": true ak ide o predaj jedného konkrétneho hudobného nástroja alebo techniky (gitara, klávesy, syntetizátor, kombo, bicie, mikrofón, efekt). Daj false, ak ide o dopyt ("Kúpim"), prenájom, výučbu, nefunkčný kus na diely, samotný obal/kufor, alebo zoznam viacerých rôznych vecí v jednom inzeráte.
    - "exact_model": Presný názov značky a modelu vhodný na vyhľadanie v e-shope Thomann.de (napr. "Fender Player Stratocaster", "Yamaha P-45", "Boss Katana 50 MkII"). Ak sa presný model nedá určiť, vráť "".
    """

  try:
    response = client.models.generate_content(
        model="gemini-2.5-flash",
        contents=prompt,
        config=types.GenerateContentConfig(
            response_mime_type="application/json",
            temperature=0.1,
        ),
    )
    return json.loads(response.text)
  except Exception as e:
    print(f"[AI Chyba]: {e}")
    return {"is_valid_sale": False, "exact_model": ""}


def get_market_prices(model_query: str, browser_context) -> dict:
  prices = {"thomann_new": None, "muziker_new": None, "reverb_used": None}
  encoded_query = quote_plus(model_query)
  page = browser_context.new_page()

  try:
    thomann_url = (
        f"https://www.thomann.de/intl/search_dir.html?sw={encoded_query}"
    )
    page.goto(thomann_url, timeout=15000)
    page.wait_for_selector("div.product", timeout=5000)

    products = page.query_selector_all("div.product")[:3]
    for prod in products:
      title_el = prod.query_selector(".product__title")
      price_el = prod.query_selector(".fx-typography-price-primary")
      if title_el and price_el:
        prod_title = title_el.inner_text().lower()
        if any(
            bad in prod_title
            for bad in ["bag", "case", "cover", "deck", "stand", "strap"]
        ):
          continue
        raw_price = price_el.inner_text()
        clean_price = re.sub(r"[^\d,]", "", raw_price).replace(",", ".")
        if clean_price:
          prices["thomann_new"] = float(clean_price)
          break
  except Exception:
    pass

  if not prices["thomann_new"]:
    try:
      muziker_url = f"https://www.muziker.sk/search?q={encoded_query}"
      page.goto(muziker_url, timeout=15000)
      page.wait_for_selector(".product-tile", timeout=5000)
      price_el = page.query_selector(".product-tile .price")
      if price_el:
        raw_price = price_el.inner_text()
        clean_price = re.sub(r"[^\d,]", "", raw_price).replace(",", ".")
        if clean_price:
          prices["muziker_new"] = float(clean_price)
    except Exception:
      pass

  page.close()

  try:
    reverb_url = f"https://api.reverb.com/api/listings?query={encoded_query}&condition=used&per_page=5"
    headers = {
        "Accept": "application/hal+json",
        "Accept-Version": "3.0",
        "Accept-Language": "en",
        "X-Display-Currency": "EUR",
    }
    r = requests.get(reverb_url, headers=headers, timeout=10)
    if r.status_code == 200:
      listings = r.json().get("listings", [])
      valid_prices = [
          float(item["price"]["amount"])
          for item in listings
          if "price" in item and "amount" in item["price"]
      ]
      if valid_prices:
        prices["reverb_used"] = round(sum(valid_prices) / len(valid_prices), 2)
  except Exception:
    pass

  return prices


def save_deal(
    ad: dict, model_name: str, prices: dict, ref_price: float, ratio: float
):
  file_exists = os.path.isfile(CSV_OUTPUT)
  with open(CSV_OUTPUT, mode="a", newline="", encoding="utf-8") as f:
    writer = csv.writer(f)
    if not file_exists:
      writer.writerow([
          "Dátum",
          "Model",
          "Cena Bazoš (€)",
          "Referenčná cena (€)",
          "Pomer (%)",
          "Thomann (€)",
          "Muziker (€)",
          "Reverb priemer (€)",
          "URL",
      ])
    writer.writerow([
        time.strftime("%Y-%m-%d %H:%M"),
        model_name,
        ad["price"],
        ref_price,
        f"{round(ratio * 100, 1)}%",
        prices["thomann_new"] or "-",
        prices["muziker_new"] or "-",
        prices["reverb_used"] or "-",
        ad["url"],
    ])

  msg = (
      f"🔥 NÁJDENÝ ÚLOVOK ({round(ratio * 100)}% ceny)!\n"
      f"🎸 Nástroj: {model_name}\n"
      f"💰 Bazoš cena: {ad['price']} € (Bežná cena: {ref_price} €)\n"
      f"🔗 Link: {ad['url']}"
  )
  print("\n" + msg + "\n")

  if TELEGRAM_BOT_TOKEN and TELEGRAM_CHAT_ID:
    tg_url = f"https://api.telegram.org/bot{TELEGRAM_BOT_TOKEN}/sendMessage"
    requests.post(tg_url, json={"chat_id": TELEGRAM_CHAT_ID, "text": msg})


def main():
  seen_ads = load_seen_ads()
  ads = fetch_bazos_ads()
  new_ads = [ad for ad in ads if ad["id"] not in seen_ads]

  print(f"Stiahnutých {len(ads)} inzerátov, z toho {len(new_ads)} nových.")

  if not new_ads:
    return

  with sync_playwright() as p:
    browser = p.chromium.launch(headless=True)
    context = browser.new_context(
        user_agent=(
            "Mozilla/5.0 (Windows NT 10.0; Win64; x64) AppleWebKit/537.36 (KHTML,"
            " like Gecko) Chrome/122.0.0.0 Safari/537.36"
        )
    )

    for ad in new_ads:
      seen_ads.add(ad["id"])
      print(f"Analyzujem: {ad['title']} ({ad['price']} €)")

      ai_data = parse_ad_with_ai(ad["title"], ad["description"])
      if not ai_data.get("is_valid_sale") or not ai_data.get("exact_model"):
        print(" -> Preskakujem (nie je konkrétny nástroj / chýba model).")
        continue

      model_name = ai_data["exact_model"]
      print(f" -> Rozpoznaný model: {model_name}")

      prices = get_market_prices(model_name, context)
      ref_price = (
          prices["thomann_new"]
          or prices["muziker_new"]
          or prices["reverb_used"]
      )

      if not ref_price:
        print(" -> Nepodarilo sa nájsť referenčnú cenu.")
        continue

      ratio = ad["price"] / ref_price
      print(
          f" -> Bazoš: {ad['price']} € | Ref: {ref_price} €"
          f" ({round(ratio * 100, 1)}%)"
      )

      if ratio <= DISCOUNT_THRESHOLD:
        save_deal(ad, model_name, prices, ref_price, ratio)

      time.sleep(2)

    browser.close()

  save_seen_ads(seen_ads)


if __name__ == "__main__":
  main()
