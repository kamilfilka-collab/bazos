import csv
import json
import os
import re
import time
from urllib.parse import quote_plus
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

DISCOUNT_THRESHOLD = 0.60  # Hľadáme cenu <= 60 % bežnej ceny
MIN_PRICE_EUR = 40  # Ignorovať lacné drobnosti pod 40 €
CSV_OUTPUT = "vyhodne_inzeraty.csv"
SEEN_FILE = "seen_ads.txt"

if not GEMINI_API_KEY:
  raise ValueError("Chýba GEMINI_API_KEY! Nastav ho v GitHub Secrets.")

client = genai.Client(api_key=GEMINI_API_KEY)


# ==========================================
# 1. PAMÄŤ UŽ VIDENÝCH INZERÁTOV
# ==========================================
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
# 2. STIAHNUTIE INZERÁTOV Z BAZOŠ.SK
# ==========================================
def fetch_bazos_ads():
  url = "https://hudba.bazos.sk/"
  headers = {
      "User-Agent": (
          "Mozilla/5.0 (Windows NT 10.0; Win64; x64) AppleWebKit/537.36"
      )
  }
  response = requests.get(url, headers=headers, timeout=15)
  response.raise_for_status()

  soup = BeautifulSoup(response.text, "html.parser")
  ads = []

  for item in soup.select("div.inzeraty.inzeratflex"):
    title_el = item.select_one("h2.nadpis a")
    desc_el = item.select_one("div.popis")
    price_el = item.select_one("div.inzeratycena b")

    if not (title_el and desc_el and price_el):
      continue

    link = "https://hudba.bazos.sk" + title_el["href"]
    match = re.search(r"/inzerat/(\d+)/", link)
    ad_id = match.group(1) if match else link

    price_text = (
        price_el.get_text(strip=True).replace(" ", "").replace("€", "")
    )
    if not price_text.isdigit():
      continue

    price = float(price_text)
    if price < MIN_PRICE_EUR:
      continue

    ads.append({
        "id": ad_id,
        "title": title_el.get_text(strip=True),
        "description": desc_el.get_text(strip=True),
        "price": price,
        "url": link,
    })

  return ads


# ==========================================
# 3. AI ANALÝZA INZERÁTU
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


# ==========================================
# 4. ZISTENIE CIEN (THOMANN / MUZIKER / REVERB)
# ==========================================
def get_market_prices(model_query: str, browser_context) -> dict:
  prices = {"thomann_new": None, "muziker_new": None, "reverb_used": None}
  encoded_query = quote_plus(model_query)
  page = browser_context.new_page()

  # --- A) Thomann.de ---
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

  # --- B) Muziker.sk (Záloha) ---
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

  # --- C) Reverb.com API (Použité kusy) ---
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


# ==========================================
# 5. ULOŽENIE ÚLOVKU A NOTIFIKÁCIA
# ==========================================
def save_deal(ad: dict, model_name: str, prices: dict, ref_price: float, ratio: float):
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


# ==========================================
# JEDNORAZOVÝ BEH PRE GITHUB ACTIONS
# ==========================================
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
            "Mozilla/5.0 (Windows NT 10.0; Win64; x64) AppleWebKit/537.36"
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
          f" -> Bazoš: {ad['price']} € | Ref: {ref_price} € ({round(ratio * 100, 1)}%)"
      )

      if ratio <= DISCOUNT_THRESHOLD:
        save_deal(ad, model_name, prices, ref_price, ratio)

      time.sleep(2)

    browser.close()

  save_seen_ads(seen_ads)


if __name__ == "__main__":
  main()
