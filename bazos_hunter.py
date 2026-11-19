import csv
import json
import os
import re
import statistics
import time
from urllib.parse import quote_plus, urljoin
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

# Model Gemini (opravený názov)
GEMINI_MODEL = "gemini-3.8-flash"

# LOGIKA MOŽNOSTI A (Porovnávanie voči reálnej BAZAROVEJ cene):
# 1. Ak poznáme cenu novej gitary, jej bežná bazarová hodnota je ~70 % ceny novej.
NEW_TO_USED_FACTOR = 0.70
# 2. Úlovok hlásime vtedy, ak je cena na Bazoši <= 75 % bežnej bazarovej hodnoty
#    (t.j. o 25 % a viac lacnejšia než bežná cena použitej na Reverbe / bazári).
USED_DEAL_THRESHOLD = 0.75

MIN_PRICE_EUR = 60  # Elektrické gitary pod 60 € bývajú hračky alebo vraky
CSV_OUTPUT = "vyhodne_inzeraty.csv"
SEEN_FILE = "seen_ads.txt"

# Slová príslušenstva a dielov, ktoré pri hľadaní ceny musíme ignorovať
BAD_WORDS = [
    "bag", "gigbag", "case", "cover", "stand", "strap", "cable",
    "power supply", "pickup", "pickguard", "neck", "body", "bridge",
    "tremolo", "tuner", "tuning", "knob", "switch", "plate", "saddle",
    "string", "nut", "harness", "loaded", "empty", "box only", "broken",
    "for parts", "defekt", "mini", "ukulele", "keychain", "shirt",
]

if not GEMINI_API_KEY:
  raise ValueError("Chýba GEMINI_API_KEY! Nastav ho v GitHub Secrets.")

client = genai.Client(api_key=GEMINI_API_KEY)


# ==========================================
# SPRÁVA VIDENÝCH INZERÁTOV (so zachovaním poradia)
# ==========================================
def load_seen_ads() -> list[str]:
  if not os.path.exists(SEEN_FILE):
    return []
  with open(SEEN_FILE, "r", encoding="utf-8") as f:
    return [line.strip() for line in f if line.strip()]


def save_seen_ads(seen_list: list[str]):
  # Zachováme unikátne ID v presnom poradí a necháme posledných 1500
  unique_ordered = list(dict.fromkeys(seen_list))[-1500:]
  with open(SEEN_FILE, "w", encoding="utf-8") as f:
    for ad_id in unique_ordered:
      f.write(f"{ad_id}\n")


# ==========================================
# 1. SŤAHOVANIE BAZOŠU (Sekcia: Gitary)
# ==========================================
def parse_bazos_html(html_text: str) -> list[dict]:
  """Priame parsovanie sekcie https://hudba.bazos.sk/gitary/"""
  ads = []
  soup = BeautifulSoup(html_text, "html.parser")
  inzeraty = soup.select("div.inzeraty.inzeratyflex")

  for item in inzeraty:
    title_el = item.select_one("div.inzeratynadpis h2.nadpis a")
    if not title_el:
      continue

    title = title_el.get_text(strip=True)
    href = title_el.get("href", "")
    url = urljoin("https://hudba.bazos.sk", href)

    match_id = re.search(r"/inzerat/(\d+)/", url)
    ad_id = match_id.group(1) if match_id else url

    price_el = item.select_one("div.inzeratycena b")
    if not price_el:
      continue

    price_raw = price_el.get_text(strip=True)
    price_digits = re.sub(r"[^\d]", "", price_raw)
    if not price_digits:
      continue

    price = float(price_digits)
    if price < MIN_PRICE_EUR:
      continue

    desc_el = item.select_one("div.inzeratynadpis div.popis")
    desc = desc_el.get_text(" ", strip=True) if desc_el else ""

    ads.append({
        "id": str(ad_id),
        "title": title,
        "description": desc,
        "price": price,
        "url": url,
    })
  return ads


def parse_rss_xml(xml_text: str) -> list[dict]:
  """Záložné parsovanie cez RSS feed"""
  ads = []
  try:
    root = ET.fromstring(xml_text)
    for item in root.findall(".//item"):
      title_raw = (item.findtext("title") or "").strip()
      link = (item.findtext("link") or "").strip()
      desc_html = item.findtext("description") or ""

      match_id = re.search(r"/inzerat/(\d+)/", link)
      ad_id = match_id.group(1) if match_id else link

      # Opravený regex: zvládne aj "€", "EUR", ",-" na konci titulku
      price_match = re.search(
          r"[:\-]\s*([\d\s]+)\s*(?:€|EUR|eur|,-)?\s*$", title_raw
      )
      if not price_match:
        continue

      price_str = re.sub(r"\s+", "", price_match.group(1))
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
          "id": str(ad_id),
          "title": clean_title,
          "description": clean_desc,
          "price": price,
          "url": link,
      })
  except Exception as e:
    print(f"Chyba pri spracovaní RSS XML: {e}")
  return ads


def fetch_bazos_ads() -> list[dict]:
  headers = {
      "User-Agent": (
          "Mozilla/5.0 (Windows NT 10.0; Win64; x64) AppleWebKit/537.36 "
          "(KHTML, like Gecko) Chrome/124.0.0.0 Safari/537.36"
      ),
      "Accept-Language": "sk-SK,sk;q=0.9,cs;q=0.8,en;q=0.7",
  }

  # 1. Pokus: Priamo podkategória GITARY (prvé 2 strany = 40 najnovších inzerátov gitár)
  all_ads = []
  direct_urls = [
      "https://hudba.bazos.sk/gitary/",
      "https://hudba.bazos.sk/gitary/20/",
  ]
  for url in direct_urls:
    try:
      print(f"Sťahujem priamo sekciu Gitary: {url} ...")
      r = requests.get(url, headers=headers, timeout=15)
      if r.status_code == 200:
        page_ads = parse_bazos_html(r.text)
        all_ads.extend(page_ads)
    except Exception as e:
      print(f" -> Priame stiahnutie zlyhalo: {e}")

  if all_ads:
    print(f" -> Úspech! Nájdených {len(all_ads)} inzerátov v sekcii Gitary.")
    return all_ads

  # 2. Záložný pokus cez RSS alebo proxy (ak by Bazoš blokoval IP GitHubu)
  rss_urls = [
      "https://www.bazos.sk/rss.php?rub=hu&kat=gitary",
      f"https://api.allorigins.win/raw?url={quote_plus('https://hudba.bazos.sk/gitary/')}",
      f"https://corsproxy.io/?{quote_plus('https://www.bazos.sk/rss.php?rub=hu')}",
  ]
  for url in rss_urls:
    try:
      print(f"Skúšam záložný zdroj: {url[:50]}...")
      r = requests.get(url, headers=headers, timeout=15)
      if r.status_code == 200:
        if "<item>" in r.text:
          ads = parse_rss_xml(r.text)
        else:
          ads = parse_bazos_html(r.text)
        if ads:
          print(f" -> Úspech cez zálohu! Nájdených {len(ads)} inzerátov.")
          return ads
    except Exception as e:
      print(f" -> Záložný pokus zlyhal: {e}")

  return []


# ==========================================
# 2. HROMADNÁ AI ANALÝZA (IBA ELEKTRICKÉ GITARY)
# ==========================================
def parse_ads_batch_with_ai(ads: list[dict]) -> dict | None:
  items_for_prompt = [
      {
          "id": a["id"],
          "title": a["title"],
          "description": a["description"][:350],
          "price_eur": a["price"],
      }
      for a in ads
  ]

  prompt = f"""
Analyzuj tento zoznam inzerátov z Bazošu:
{json.dumps(items_for_prompt, ensure_ascii=False)}

Hľadáme VÝHRADNE ELEKTRICKÉ GITARY (6-strunové, 7-strunové, 8-strunové elektrické gitary, vrátane semihollow/hollowbody elektrických gitár).

Pre každý inzerát vráť objekt v JSON poli s týmito poľami:
- "id": rovnaké ID inzerátu ako na vstupe (string)
- "is_electric_guitar": true IBA ak ide o predaj jednej konkrétnej, funkčnej elektrickej gitary so známou značkou a modelom.
  Daj false ak ide o:
  * akustickú gitaru, elektro-akustickú gitaru, klasickú gitaru / španielku, ukulele
  * basgitaru (4/5/6 strunovú basu)
  * kombo, zosilňovač, reprobox, multiefekt, pedál, snímače, krk, telo, kufor, obal
  * dopyt ("Kúpim"), výmenu bez predaja, poškodený vrak na diely, alebo lacnú čínsku kópiu/fejk (napr. falošný Gibson/Fender).
  * nešpecifikovanú gitaru, pri ktorej sa nedá určiť presný model (napr. len "Elektrická gitara Ibanez" alebo "Fender Stratocaster" bez uvedenia série ako Player, Standard, American Pro, Squier Affinity a pod.).
- "brand": Značka gitary (napr. "Fender", "Squier", "Ibanez", "Gibson", "Epiphone", "ESP LTD", "PRS", "Schecter", "Jackson", "Harley Benton", "Cort", "Yamaha").
- "exact_model": Presný názov značky a modelu vhodný na vyhľadanie ceny na Reverb.com a Thomann.de (bez farby a bez zbytočných slov ako 'predám', napr. "Fender Player Stratocaster", "Ibanez RG421", "Epiphone Les Paul Standard 60s", "PRS SE Custom 24", "ESP LTD EC-256"). Ak model nie je jasný, vráť "".
- "must_have_tokens": Pole 2 až 4 kľúčových slov (malými písmenami), ktoré sa MUSIA nachádzať v názve produktu na e-shope/Reverbe, aby sme mali istotu, že porovnávame rovnaký model a nie inú sériu (napr. pre "Fender Player Stratocaster" daj ["fender", "player", "stratocaster"]; pre "ESP LTD EC-256" daj ["ltd", "ec-256"]).
"""

  for attempt in range(3):
    try:
      print(f"Posielam {len(ads)} inzerátov do Gemini ({GEMINI_MODEL})...")
      response = client.models.generate_content(
          model=GEMINI_MODEL,
          contents=prompt,
          config=types.GenerateContentConfig(
              response_mime_type="application/json",
              temperature=0.1,
          ),
      )
      parsed_list = json.loads(response.text)
      return {str(item.get("id")): item for item in parsed_list}
    except Exception as e:
      print(f"[AI Pokus {attempt + 1}/3 zlyhal]: {e}")
      time.sleep(10)

  return None


# ==========================================
# 3. POMOCNÉ FUNKCIE PRE CENY A KONTROLU ZHODY NÁZVU
# ==========================================
def parse_euro_price(raw_text: str) -> float | None:
  cleaned = re.sub(r"[^\d.,]", "", raw_text).strip(".,")
  if not cleaned:
    return None

  if "," in cleaned and "." in cleaned:
    if cleaned.rfind(",") > cleaned.rfind("."):
      cleaned = cleaned.replace(".", "").replace(",", ".")
    else:
      cleaned = cleaned.replace(",", "")
  elif "." in cleaned:
    parts = cleaned.split(".")
    if len(parts[-1]) == 3:
      cleaned = cleaned.replace(".", "")
  elif "," in cleaned:
    parts = cleaned.split(",")
    if len(parts[-1]) == 3:
      cleaned = cleaned.replace(",", "")
    else:
      cleaned = cleaned.replace(",", ".")

  try:
    return float(cleaned)
  except ValueError:
    return None


def normalize_text(text: str) -> str:
  """Zjednotí pomlčky a medzery pre spoľahlivé porovnávanie modelov (napr. EC-256 vs EC 256)."""
  return re.sub(r"[\-_/\s]+", " ", text.lower()).strip()


def title_matches_model(
    product_title: str, model_query: str, must_have_tokens: list[str]
) -> bool:
  """Overí, či nájdený produkt na Reverbe/Thomanne je naozaj hľadaná gitara a nie iný model alebo diel."""
  norm_title = normalize_text(product_title)

  # 1. Vyradíme príslušenstvo, obaly, snímače, krky
  for bad in BAD_WORDS:
    if re.search(rf"\b{re.escape(bad)}\b", norm_title):
      return False

  # 2. Ak má hľadaný model Squier/Epiphone/LTD/SE/Gio, nesmie sa zameniť za drahý Fender/Gibson/ESP/PRS
  query_norm = normalize_text(model_query)
  budget_lines = ["squier", "epiphone", "ltd", "gio", "se", "affinity", "bullet", "sonic", "js"]
  for b_word in budget_lines:
    if re.search(rf"\b{b_word}\b", norm_title) and not re.search(rf"\b{b_word}\b", query_norm):
      return False
    if re.search(rf"\b{b_word}\b", query_norm) and not re.search(rf"\b{b_word}\b", norm_title):
      return False

  # 3. Kontrola kľúčových tokenov modelu
  tokens = must_have_tokens if must_have_tokens else query_norm.split()
  matched = 0
  for tok in tokens:
    norm_tok = normalize_text(tok)
    if not norm_tok:
      continue
    if norm_tok in norm_title:
      matched += 1

  # Všetky povinné tokeny (alebo aspoň 2 pri dlhších názvoch) musia sedieť
  required = len(tokens) if len(tokens) <= 3 else len(tokens) - 1
  return matched >= max(1, required)


# ==========================================
# 4. ZISTENIE CIEN (REVERB PRIMÁRNE + THOMANN/MUZIKER KONTROLA)
# ==========================================
def get_reverb_used_price(
    model_query: str, must_have_tokens: list[str], bazo_price: float
) -> tuple[float | None, int]:
  """
  Vráti (mediánová_cena_na_reverbe, počet_zhodných_inzerátov).
  Filtruje len inzeráty, ktorých titulok reálne zodpovedá hľadanému modelu.
  """
  encoded_query = quote_plus(model_query)
  reverb_url = (
      f"https://api.reverb.com/api/listings?query={encoded_query}"
      "&condition=used&per_page=20"
  )
  headers = {
      "Accept": "application/hal+json",
      "Accept-Version": "3.0",
      "Accept-Language": "en",
      "X-Display-Currency": "EUR",
  }

  try:
    r = requests.get(reverb_url, headers=headers, timeout=12)
    if r.status_code != 200:
      return None, 0

    listings = r.json().get("listings", [])
    valid_prices = []

    for item in listings:
      title = item.get("title", "")
      price_info = item.get("price", {})
      amount = price_info.get("amount")
      if not amount or not title:
        continue

      if not title_matches_model(title, model_query, must_have_tokens):
        continue

      val = float(amount)
      # Odfiltrujeme extrémne nízke sumy (často len záloha alebo chybný diel)
      if val >= max(MIN_PRICE_EUR, bazo_price * 0.35):
        valid_prices.append(val)

    if not valid_prices:
      return None, 0

    # Ak máme 3 a viac cien, orežeme extrémny vrch (vintage/custom shop anomálie)
    valid_prices.sort()
    if len(valid_prices) >= 4:
      valid_prices = valid_prices[:-1]  # zahodíme najdrahší extrém

    median_price = round(statistics.median(valid_prices), 2)
    return median_price, len(valid_prices)
  except Exception as e:
    print(f"   [Reverb chyba]: {e}")
    return None, 0


def get_new_eshop_prices(
    model_query: str, must_have_tokens: list[str], browser_context
) -> tuple[float | None, float | None]:
  """Zistí cenu NOVEJ gitary na Thomann.de a Muziker.sk s prísnou kontrolou názvu modelu."""
  thomann_price = None
  muziker_price = None
  encoded_query = quote_plus(model_query)
  page = browser_context.new_page()

  # --- 1. THOMANN.DE ---
  try:
    thomann_url = f"https://www.thomann.de/intl/search_dir.html?sw={encoded_query}"
    page.goto(thomann_url, timeout=15000, wait_until="domcontentloaded")
    page.wait_for_timeout(1500)

    # Thomann používa .fx-product-list-entry alebo div.product
    products = page.query_selector_all(".fx-product-list-entry, div.product")[:6]
    for prod in products:
      title_el = prod.query_selector(
          ".product__title, .fx-product-list-entry__title, a.product__content"
      )
      price_el = prod.query_selector(
          ".fx-typography-price-primary, .product__price-primary"
      )
      if title_el and price_el:
        prod_title = title_el.inner_text().strip()
        if not title_matches_model(prod_title, model_query, must_have_tokens):
          continue
        val = parse_euro_price(price_el.inner_text())
        if val and val >= MIN_PRICE_EUR:
          thomann_price = val
          break
  except Exception:
    pass

  # --- 2. MUZIKER.SK (ak Thomann nenašiel) ---
  if not thomann_price:
    try:
      muziker_url = f"https://www.muziker.sk/search?q={encoded_query}"
      page.goto(muziker_url, timeout=15000, wait_until="domcontentloaded")
      page.wait_for_timeout(1500)

      tiles = page.query_selector_all(".product-tile, [data-product-tile]")[:6]
      for tile in tiles:
        title_el = tile.query_selector("h3, h4, .product-tile__title, a[title]")
        price_el = tile.query_selector(".price, .product-tile__price")
        if title_el and price_el:
          prod_title = title_el.inner_text().strip() or (
              title_el.get_attribute("title") or ""
          )
          if not title_matches_model(prod_title, model_query, must_have_tokens):
            continue
          val = parse_euro_price(price_el.inner_text())
          if val and val >= MIN_PRICE_EUR:
            muziker_price = val
            break
    except Exception:
      pass

  page.close()
  return thomann_price, muziker_price


def compute_reference_used_price(prices: dict) -> tuple[float | None, str]:
  """
  MOŽNOSŤ A: Vypočíta reálnu BAZAROVÚ referenčnú cenu použitej gitary.
  - Ak máme Reverb (použité) aj E-shop (nová):
    vezmeme nižšiu z hodnôt `Reverb_medián` a `Nová_cena * 0.70`,
    pretože použitá gitara nemá mať vyššiu referenčnú bazarovú cenu než 70 % aktuálnej novej v obchode.
  - Ak máme len Reverb: použijeme Reverb medián.
  - Ak sa model na Reverbe nenašiel, ale predáva sa nový v e-shope:
    odhadneme bazarovú cenu ako `Nová_cena * 0.70`.
  """
  reverb_used = prices["reverb_used"]
  eshop_new = prices["thomann_new"] or prices["muziker_new"]
  estimated_used_from_new = (
      round(eshop_new * NEW_TO_USED_FACTOR, 2) if eshop_new else None
  )

  if reverb_used and estimated_used_from_new:
    if reverb_used <= estimated_used_from_new:
      return reverb_used, f"Reverb medián ({prices['reverb_count']} ks)"
    else:
      return (
          estimated_used_from_new,
          f"70 % z novej v e-shope ({eshop_new} €), Reverb bol vyšší ({reverb_used} €)",
      )
  elif reverb_used:
    return reverb_used, f"Reverb medián ({prices['reverb_count']} ks)"
  elif estimated_used_from_new:
    return (
        estimated_used_from_new,
        f"70 % z novej ceny v e-shope ({eshop_new} €)",
    )

  return None, ""


# ==========================================
# 5. UKLADANIE A TELEGRAM NOTIFIKÁCIA
# ==========================================
def save_deal(
    ad: dict,
    model_name: str,
    prices: dict,
    ref_used_price: float,
    ref_source: str,
    ratio: float,
):
  file_exists = os.path.isfile(CSV_OUTPUT)
  with open(CSV_OUTPUT, mode="a", newline="", encoding="utf-8") as f:
    writer = csv.writer(f)
    if not file_exists:
      writer.writerow([
          "Dátum",
          "Model",
          "Cena Bazoš (€)",
          "Bežná bazarová cena (€)",
          "Pomer k bazarovej (%)",
          "Zdroj referencie",
          "Reverb medián (€)",
          "Thomann nová (€)",
          "Muziker nová (€)",
          "URL",
      ])
    writer.writerow([
        time.strftime("%Y-%m-%d %H:%M"),
        model_name,
        ad["price"],
        ref_used_price,
        f"{round(ratio * 100, 1)}%",
        ref_source,
        prices["reverb_used"] or "-",
        prices["thomann_new"] or "-",
        prices["muziker_new"] or "-",
        ad["url"],
    ])

  eshop_new = prices["thomann_new"] or prices["muziker_new"]
  new_info = f" (Nová v e-shope: {eshop_new} €)" if eshop_new else ""

  msg = (
      f"🔥 ELEKTRICKÁ GITARA POD CENOU ({round(ratio * 100)}% bazarovej hodnoty)!\n"
      f"🎸 Model: {model_name}\n"
      f"💰 Bazoš cena: {ad['price']} €\n"
      f"📉 Bežná bazarová cena: {ref_used_price} € [{ref_source}]{new_info}\n"
      f"📝 Inzerát: {ad['title']}\n"
      f"🔗 Link: {ad['url']}"
  )
  print("\n" + msg + "\n")

  if TELEGRAM_BOT_TOKEN and TELEGRAM_CHAT_ID:
    try:
      tg_url = f"https://api.telegram.org/bot{TELEGRAM_BOT_TOKEN}/sendMessage"
      requests.post(
          tg_url, json={"chat_id": TELEGRAM_CHAT_ID, "text": msg}, timeout=10
      )
    except Exception as e:
      print(f"Chyba pri odosielaní na Telegram: {e}")


# ==========================================
# HLAVNÝ BEH PROGRAMU
# ==========================================
def main():
  seen_ads = load_seen_ads()
  seen_set = set(seen_ads)

  ads = fetch_bazos_ads()
  new_ads = [ad for ad in ads if ad["id"] not in seen_set]

  print(f"Stiahnutých {len(ads)} inzerátov, z toho {len(new_ads)} nových.")

  if not new_ads:
    return

  ai_results = parse_ads_batch_with_ai(new_ads)
  if ai_results is None:
    print("AI analýza momentálne zlyhala, skúsim pri ďalšom behu.")
    return

  with sync_playwright() as p:
    browser = p.chromium.launch(headless=True)
    context = browser.new_context(
        user_agent=(
            "Mozilla/5.0 (Windows NT 10.0; Win64; x64) AppleWebKit/537.36 "
            "(KHTML, like Gecko) Chrome/124.0.0.0 Safari/537.36"
        )
    )

    for ad in new_ads:
      seen_ads.append(ad["id"])
      ai_data = ai_results.get(str(ad["id"]), {})

      print(f"\nAnalyzujem: {ad['title']} ({ad['price']} €)")

      if not ai_data.get("is_electric_guitar") or not ai_data.get("exact_model"):
        print(" -> Preskakujem (nie je konkrétna elektrická gitara / chýba model).")
        continue

      model_name = ai_data["exact_model"].strip()
      must_have_tokens = ai_data.get("must_have_tokens") or []
      print(f" -> Elektrická gitara: {model_name} (tokeny: {must_have_tokens})")

      # 1. Zistíme cenu použitých kusov na Reverbe
      reverb_median, reverb_count = get_reverb_used_price(
          model_name, must_have_tokens, ad["price"]
      )

      # 2. Zistíme cenu novej na Thomanne / Muzikeri (pre kontrolu stropu)
      thomann_new, muziker_new = get_new_eshop_prices(
          model_name, must_have_tokens, context
      )

      prices = {
          "reverb_used": reverb_median,
          "reverb_count": reverb_count,
          "thomann_new": thomann_new,
          "muziker_new": muziker_new,
      }

      ref_used_price, ref_source = compute_reference_used_price(prices)

      if not ref_used_price:
        print(" -> Nepodarilo sa nájsť spoľahlivú bazarovú ani e-shopovú cenu.")
        continue

      ratio = ad["price"] / ref_used_price
      print(
          f" -> Bazoš: {ad['price']} € | Bežná bazarová: {ref_used_price} € "
          f"[{ref_source}] -> Pomer: {round(ratio * 100, 1)}%"
      )

      if ratio <= USED_DEAL_THRESHOLD:
        save_deal(ad, model_name, prices, ref_used_price, ref_source, ratio)

      time.sleep(2)

    browser.close()

  save_seen_ads(seen_ads)


if __name__ == "__main__":
  main()
