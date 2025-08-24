im111port os
import sys
import json
import hashlib
import time
import subprocess
import requests
import tweepy
from bs4 import BeautifulSoup
from PIL import Image
from io import BytesIO
try:
    import google.generativeai as genai
except ImportError:
    genai = None

# Constants
SITE_URL = "https://horizon-libre.net"
ARTICLES_DIR = "article"
MAX_TWEET_LENGTH = 280
UTM_PARAMS = "?utm_source=twitter&utm_medium=social&utm_campaign=autotweet"
GEMINI_MODEL_DEFAULT = "gemini-1.5-flash"
PAUSE_BETWEEN_TWEETS = 10  # secondes
EMPTY_TREE_SHA = "4b825dc642cb6eb9a060e54bf8d69288fbee4904"
MAX_ARTICLES_PER_RUN = 5  # Limite pour éviter rate limits

# Logging function
def log(message):
    print(message, flush=True)

# Memory functions for deduplication
def get_memory_key(article_path):
    return hashlib.sha256(article_path.encode()).hexdigest().lower()

def has_been_seen(key, blobs_url, token):
    if not blobs_url or not token:
        log("Mémoire non configurée, continue sans check")
        return False
    try:
        url = f"{blobs_url}/{key}"
        response = requests.get(url, headers={"X-AURORE-TOKEN": token})
        return response.status_code == 200
    except Exception as e:
        log(f"Memory check failed: {e}")
        return False

def mark_as_seen(key, blobs_url, token):
    if not blobs_url or not token:
        return
    try:
        url = f"{blobs_url}/{key}"
        requests.put(url, data="1", headers={"X-AURORE-TOKEN": token})
    except Exception as e:
        log(f"Memory mark failed: {e}")

# Read GitHub event
def read_github_event():
    event_path = os.environ.get("GITHUB_EVENT_PATH")
    if event_path:
        with open(event_path, "r") as f:
            return json.load(f)
    return None

# Detect new articles
def detect_new_articles():
    event = read_github_event()
    log(f"Événement GitHub : {event}")
    if event and event.get("action") == "new-article-published":
        payload = event.get("client_payload", {})
        articles = payload.get("articles", [])[:MAX_ARTICLES_PER_RUN]
        log(f"Articles détectés via dispatch: {articles}")
        return articles
    else:
        try:
            try:
                prev_sha = subprocess.check_output(["git", "rev-parse", "HEAD~1"]).decode().strip()
            except subprocess.CalledProcessError:
                prev_sha = EMPTY_TREE_SHA
            diff_output = subprocess.check_output(["git", "diff", "--diff-filter=A", "--name-only", prev_sha, "HEAD"]).decode().splitlines()
            articles = [f for f in diff_output if f.startswith(ARTICLES_DIR + "/") and f.endswith(".html")][:MAX_ARTICLES_PER_RUN]
            log(f"Articles détectés via push: {articles}")
            return articles
        except Exception as e:
            log(f"Git diff failed: {e}")
            return []

# Parse HTML for title and category
def parse_article(article_path):
    try:
        with open(article_path, "r", encoding="utf-8") as f:
            soup = BeautifulSoup(f.read(), "html.parser")
        title = soup.title.string.strip() if soup.title else "Untitled"
        category = None
        meta_section = soup.find("meta", {"property": "article:section"})
        if meta_section:
            category = meta_section["content"].strip()
        else:
            meta_category = soup.find("meta", {"name": "category"})
            if meta_category:
                category = meta_category["content"].strip()
        log(f"Titre extrait: {title}, Catégorie: {category}")
        return title, category
    except Exception as e:
        log(f"Parsing failed for {article_path}: {e}")
        return None, None

# Generate hashtags
def generate_hashtags(title, category):
    hashtags = ["#HorizonLibre"]
    if category:
        hashtags.append(f"#{category.replace(' ', '').lower()}")
    elif title:
        words = title.split()
        if words:
            hashtags.append(f"#{words[0].lower()}")
    return " ".join(set(hashtags[:2]))

# Append UTM if enabled
def append_utm(url):
    if os.environ.get("ENABLE_UTM"):
        return url + UTM_PARAMS
    return url

# Safe trim to max length
def safe_trim(text, max_len=MAX_TWEET_LENGTH):
    if len(text) <= max_len:
        return text
    return text[:max_len - 3] + "..."

# Generate alt text
def generate_alt_text(image_data, gemini_api_key, gemini_model):
    if gemini_api_key and genai:
        try:
            genai.configure(api_key=gemini_api_key)
            model = genai.GenerativeModel(gemini_model)
            response = model.generate_content(["Describe this image briefly for accessibility.", image_data])
            alt = response.text.strip()[:1000]
            return alt if alt else "Image from article"
        except Exception as e:
            log(f"Gemini failed: {e}")
    return "Image from article"

# Find and prepare image
def find_and_prepare_image(article_path, soup):
    try:
        img_url = None
        alt = None
        og_image = soup.find("meta", {"property": "og:image"})
        if og_image:
            img_url = og_image["content"]
        else:
            twitter_image = soup.find("meta", {"name": "twitter:image"})
            if twitter_image:
                img_url = twitter_image["content"]
            else:
                image_src = soup.find("link", {"rel": "image_src"})
                if image_src:
                    img_url = image_src["href"]
                else:
                    article_tag = soup.find("article")
                    if article_tag:
                        img_tag = article_tag.find("img")
                        if img_tag:
                            img_url = img_tag["src"]
                            alt = img_tag.get("alt") or (img_tag.find_parent("figure").find("figcaption").text.strip() if img_tag.find_parent("figure") else None)
        if not img_url:
            return None, None

        if not img_url.startswith("http"):
            img_url = os.path.join(os.path.dirname(article_path), img_url)
            with open(img_url, "rb") as f:
                img_data = f.read()
        else:
            response = requests.get(img_url)
            img_data = response.content

        img = Image.open(BytesIO(img_data))
        img = img.convert("RGB")
        max_size = 4096
        if img.width > max_size or img.height > max_size:
            ratio = min(max_size / img.width, max_size / img.height)
            img = img.resize((int(img.width * ratio), int(img.height * ratio)), Image.LANCZOS)
        quality = 95
        while True:
            buffer = BytesIO()
            img.save(buffer, format="JPEG", progressive=True, quality=quality)
            size = buffer.tell()
            if size <= 4.8 * 1024 * 1024 or quality <= 50:
                break
            quality -= 5
        return buffer.getvalue(), alt
    except Exception as e:
        log(f"Image processing failed: {e}")
        return None, None

# Build tweet text
def build_tweet_text(title, hashtags, article_url):
    base = f"Nouvel article: {title}"
    tweet = f"{base} {hashtags} {article_url}"
    return safe_trim(tweet)

# Post tweet
def post_tweet(tweet_text, image_data=None, alt_text=None):
    try:
        consumer_key = os.environ["X_API_KEY"]
        consumer_secret = os.environ["X_API_SECRET"]
        access_token = os.environ["X_ACCESS_TOKEN"]
        access_token_secret = os.environ["X_ACCESS_TOKEN_SECRET"]

        auth = tweepy.OAuth1UserHandler(consumer_key, consumer_secret, access_token, access_token_secret)
        api = tweepy.API(auth)

        client = tweepy.Client(consumer_key=consumer_key, consumer_secret=consumer_secret,
                               access_token=access_token, access_token_secret=access_token_secret)

        media_ids = None
        if image_data:
            media = api.media_upload(filename="image.jpg", file=BytesIO(image_data))
            media_ids = [media.media_id]
            if alt_text:
                api.create_media_metadata(media.media_id, alt_text)

        client.create_tweet(text=tweet_text, media_ids=media_ids)
        log("Tweet posted successfully")
    except Exception as e:
        log(f"Tweet posting failed: {e}")

# Main function
def main():
    articles = detect_new_articles()
    if not articles:
        log("No new articles found")
        return

    blobs_url = os.environ.get("BLOBS_PROXY_URL")
    aurore_token = os.environ.get("AURORE_BLOBS_TOKEN")
    gemini_api_key = os.environ.get("GEMINI_API_KEY_HORIZON")
    gemini_model = os.environ.get("GEMINI_MODEL", GEMINI_MODEL_DEFAULT)

    for idx, article_path in enumerate(articles):
        key = get_memory_key(article_path)
        if has_been_seen(key, blobs_url, aurore_token):
            log(f"Skipping duplicate: {article_path}")
            continue

        title, category = parse_article(article_path)
        if not title:
            log(f"Skipping invalid article: {article_path}")
            continue

        with open(article_path, "r", encoding="utf-8") as f:
            soup = BeautifulSoup(f.read(), "html.parser")

        hashtags = generate_hashtags(title, category)
        article_url = append_utm(f"{SITE_URL}/{article_path}")
        tweet_text = build_tweet_text(title, hashtags, article_url)

        image_data, html_alt = find_and_prepare_image(article_path, soup)
        alt_text = html_alt if html_alt else generate_alt_text(image_data, gemini_api_key, gemini_model) if image_data else None

        post_tweet(tweet_text, image_data, alt_text)

        mark_as_seen(key, blobs_url, aurore_token)

        if idx < len(articles) - 1:
            time.sleep(PAUSE_BETWEEN_TWEETS)

if __name__ == "__main__":
    main()
