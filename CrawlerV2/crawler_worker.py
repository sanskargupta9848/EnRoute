import os
import time
import requests
import logging
import random
import threading
import queue
import hashlib
import traceback
from urllib.parse import urljoin, urlparse
from urllib.robotparser import RobotFileParser
import multiprocessing
import psutil
import json
from requests.adapters import HTTPAdapter
from urllib3.util.retry import Retry
from crawler_config import API_BASE_URL
from bs4 import BeautifulSoup

# Configure logging
logging.basicConfig(
    level=logging.DEBUG,
    format='%(asctime)s [%(levelname)s] [%(threadName)s] %(message)s',
    handlers=[
        logging.StreamHandler(),
        logging.FileHandler('crawler_worker.log')
    ]
)
logger = logging.getLogger(__name__)

# Maximum threads based on CPU count
MAX_THREADS = multiprocessing.cpu_count()
logger.info(f"Detected {MAX_THREADS} CPU cores; maximum thread count set to {MAX_THREADS}")

# Shared queue for GUI communication
log_queue = queue.Queue()

# HTTP session with retries
session = requests.Session()
retries = Retry(
    total=3,
    backoff_factor=1,
    status_forcelist=[429, 500, 502, 503, 504]
)
adapter = HTTPAdapter(max_retries=retries)
session.mount('http://', adapter)
session.mount('https://', adapter)

# Request headers
HEADERS = {
    'User-Agent': 'Mozilla/5.0 (Windows NT 10.0; Win64; x64) AppleWebKit/537.36 (KHTML, like Gecko) Chrome/91.0.4472.124 Safari/537.36'
}

# Rate limit tracking
domain_delays = {}

# Blacklist cache (domain -> (is_blacklisted, timestamp))
blacklist_cache = {}
BLACKLIST_CACHE_TTL = 300  # 5 minutes

# Common tags by domain
DOMAIN_TAGS = {
    'youtube.com': ['video', 'streaming', 'media', 'content', 'social', 'youtube', 'channel', 'vlog', 'entertainment', 'music'],
    'example.com': ['test', 'demo', 'example', 'web', 'sample', 'internet', 'page', 'site', 'domain', 'testing'],
    'mit.edu': ['education', 'research', 'university', 'academic', 'science', 'technology', 'learning', 'course', 'study', 'knowledge'],
    'robertspaceindustries.com': ['gaming', 'space', 'simulation', 'scifi', 'crowdfunding'],
    'steampowered.com': ['gaming', 'store', 'digital', 'distribution', 'platform'],
    'theoldnet.com': ['retro', 'internet', 'nostalgia', 'archive', 'web'],
    'archive.org': ['archive', 'internet', 'history', 'digital', 'library'],
    'timestripe.com': ['blog', 'technology', 'trends', 'internet', 'culture'],
    'web-scraping.dev': ['web', 'scraping', 'development', 'data', 'tools'],
    'data.gov': ['government', 'data', 'open', 'public', 'datasets'],
    'amazon.com': ['ecommerce', 'retail', 'shopping', 'online', 'marketplace']
}

def get_crawl_delay(url):
    """Get crawl delay from robots.txt."""
    try:
        parsed = urlparse(url)
        robots_url = f"{parsed.scheme}://{parsed.netloc}/robots.txt"
        rp = RobotFileParser()
        rp.set_url(robots_url)
        rp.read()
        delay = rp.crawl_delay(HEADERS['User-Agent']) or 1.0
        logger.debug(f"Crawl delay for {url}: {delay}s")
        return delay
    except Exception as e:
        logger.warning(f"Error checking robots.txt for {url}: {e}")
        return 1.0

def can_crawl(url):
    """Check if URL is allowed by robots.txt."""
    try:
        parsed = urlparse(url)
        robots_url = f"{parsed.scheme}://{parsed.netloc}/robots.txt"
        rp = RobotFileParser()
        rp.set_url(robots_url)
        rp.read()
        can_fetch = rp.can_fetch(HEADERS['User-Agent'], url)
        logger.debug(f"robots.txt check for {url}: {'Allowed' if can_fetch else 'Disallowed'}")
        return can_fetch
    except Exception as e:
        logger.warning(f"Error checking robots.txt for {url}: {e}")
        log_queue.put(f"Error checking robots.txt for {url}: {e}")
        return True

def is_domain_blacklisted(domain, blacklist_check_endpoint, jwt_token):
    """Check if domain is blacklisted, using cache and supporting wildcard patterns."""
    try:
        current_time = time.time()
        if domain in blacklist_cache:
            is_blacklisted, timestamp = blacklist_cache[domain]
            if current_time - timestamp < BLACKLIST_CACHE_TTL:
                logger.debug(f"Cache hit for {domain}: {'blacklisted' if is_blacklisted else 'not blacklisted'}")
                return is_blacklisted
            else:
                del blacklist_cache[domain]
                logger.debug(f"Cache expired for {domain}")
        
        headers = HEADERS.copy()
        if jwt_token:
            headers['Authorization'] = f'Bearer {jwt_token}'
        response = session.get(blacklist_check_endpoint, params={'domain': domain}, headers=headers, timeout=5)
        response.raise_for_status()
        is_blacklisted = response.json().get('blacklisted', False)
        blacklist_cache[domain] = (is_blacklisted, current_time)
        logger.info(f"Domain {domain} is {'blacklisted' if is_blacklisted else 'not blacklisted'}")
        log_queue.put(f"Domain {domain} is {'blacklisted' if is_blacklisted else 'not blacklisted'}")
        return is_blacklisted
    except requests.exceptions.HTTPError as e:
        logger.error(f"HTTP error checking blacklist for {domain}: {e.response.status_code} - {e.response.text}")
        log_queue.put(f"HTTP error checking blacklist for {domain}: {e.response.status_code}")
        blacklist_cache[domain] = (True, current_time)
        return True
    except Exception as e:
        logger.error(f"Error checking blacklist for {domain}: {e}\n{traceback.format_exc()}")
        log_queue.put(f"Error checking blacklist for {domain}: {e}")
        blacklist_cache[domain] = (True, current_time)
        return True

def respect_rate_limit(url):
    """Enforce crawl delay for the domain."""
    try:
        domain = urlparse(url).netloc
        delay = get_crawl_delay(url)
        last_crawled = domain_delays.get(domain, 0)
        current_time = time.time()
        if current_time - last_crawled < delay:
            sleep_time = delay - (current_time - last_crawled)
            time.sleep(sleep_time)
        domain_delays[domain] = time.time()
    except Exception as e:
        logger.error(f"Error enforcing rate limit for {url}: {e}\n{traceback.format_exc()}")
        time.sleep(1.0)

def sanitize_text(text):
    """Sanitize text to remove non-UTF-8 characters."""
    if not text:
        return ''
    try:
        return text.encode('utf-8', errors='ignore').decode('utf-8')[:255]
    except Exception as e:
        logger.error(f"Error sanitizing text: {e}\n{traceback.format_exc()}")
        return ''

def extract_title(url, soup=None):
    """Extract title from URL or HTML soup."""
    try:
        if soup and soup.title and soup.title.string:
            title = sanitize_text(soup.title.string.strip())
            logger.debug(f"Extracted title from HTML: {title}")
            return title
        parsed = urlparse(url)
        path = parsed.path.strip('/')
        if path:
            title = path.replace('/', ' ').replace('-', ' ').title()
            logger.debug(f"Extracted title from URL path: {title}")
            return title[:255]
        title = parsed.netloc.replace('.', ' ').title()
        logger.debug(f"Extracted title from domain: {title}")
        return title[:255]
    except Exception as e:
        logger.error(f"Error extracting title for {url}: {e}\n{traceback.format_exc()}")
        return f"{urlparse(url).netloc.replace('.', ' ').title()}"[:255]

def extract_summary(url, soup=None):
    """Extract summary from URL or HTML soup."""
    try:
        if soup:
            meta_desc = soup.find('meta', attrs={'name': 'description'})
            if meta_desc and meta_desc.get('content'):
                summary = sanitize_text(meta_desc['content'].strip())
                logger.debug(f"Extracted summary from meta description: {summary[:50]}...")
                return summary[:200]
        parsed = urlparse(url)
        query = parsed.query.replace('&', ' ').replace('=', ' ').strip()
        path = parsed.path.replace('/', ' ').replace('-', ' ').strip()
        summary = f"Web content from {parsed.netloc} {path} {query}".strip()
        logger.debug(f"Extracted summary from URL: {summary[:50]}...")
        return summary[:200]
    except Exception as e:
        logger.error(f"Error extracting summary for {url}: {e}\n{traceback.format_exc()}")
        return f"Web content from {urlparse(url).netloc}"[:200]

def extract_tags(url, soup=None, max_tags=40, min_tags=20):
    """Extract tags from URL, domain, and HTML soup."""
    try:
        tags = []
        parsed = urlparse(url)
        domain = parsed.netloc
        domain_key = domain.split('.')[-2] + '.' + domain.split('.')[-1]
        
        # Add domain-specific tags
        tags.extend(DOMAIN_TAGS.get(domain_key, []))
        
        # Extract from URL
        path = parsed.path.strip('/').replace('-', ' ').replace('_', ' ')
        query = parsed.query.replace('&', ' ').replace('=', ' ').strip()
        url_words = [w.lower() for w in f"{path} {query}".split() if w and len(w) > 2]
        tags.extend(url_words)
        
        # Add domain as tag
        tags.append(domain_key)
        
        # Deduplicate and limit
        tags = list(dict.fromkeys(tags))[:max_tags]
        if len(tags) < min_tags:
            tags.extend([f"web{i}" for i in range(min_tags - len(tags))])
        
        logger.debug(f"Extracted tags (count={len(tags)}): {','.join(tags[:5])}...")
        return ','.join(tags[:max_tags])
    except Exception as e:
        logger.error(f"Error extracting tags for {url}: {e}\n{traceback.format_exc()}")
        return ','.join([f"web{i}" for i in range(min_tags)])

def extract_urls(response, base_url, blacklist_check_endpoint, jwt_token):
    """Extract URLs from HTTP headers and HTML content."""
    try:
        urls = set()
        # Extract from headers (e.g., Location)
        if 'Location' in response.headers:
            absolute_url = urljoin(base_url, response.headers['Location'])
            parsed = urlparse(absolute_url)
            if parsed.scheme in ['http', 'https'] and not is_domain_blacklisted(parsed.netloc, blacklist_check_endpoint, jwt_token):
                urls.add(absolute_url[:2048])
        
        # Extract from HTML content
        if 'text/html' in response.headers.get('Content-Type', ''):
            soup = BeautifulSoup(response.text, 'html.parser')
            for a in soup.find_all('a', href=True):
                absolute_url = urljoin(base_url, a['href'])
                parsed = urlparse(absolute_url)
                if parsed.scheme in ['http', 'https'] and not is_domain_blacklisted(parsed.netloc, blacklist_check_endpoint, jwt_token):
                    urls.add(absolute_url[:2048])
        
        logger.debug(f"Extracted {len(urls)} new URLs from {base_url}")
        return list(urls)
    except Exception as e:
        logger.error(f"Error extracting URLs for {base_url}: {e}\n{traceback.format_exc()}")
        return []

def crawl_url(url, blacklist_check_endpoint, jwt_token, enforce_robots=True):
    """Crawl a single URL and extract data, with option to enforce robots.txt."""
    start_time = time.time()
    logger.info(f"Starting crawl for {url}")
    domain = urlparse(url).netloc
    try:
        if domain == 'wikihow.com' or is_domain_blacklisted(domain, blacklist_check_endpoint, jwt_token):
            logger.warning(f"Skipping {url} as domain {domain} is blacklisted")
            log_queue.put(f"Skipping {url} as domain {domain} is blacklisted")
            return None
    except Exception as e:
        logger.error(f"Error checking blacklist for {url}: {e}\n{traceback.format_exc()}")
        return None
    
    if enforce_robots:
        try:
            if not can_crawl(url):
                logger.warning(f"URL {url} disallowed by robots.txt")
                log_queue.put(f"URL {url} disallowed by robots.txt")
                return None
        except Exception as e:
            logger.error(f"Error checking robots.txt for {url}: {e}\n{traceback.format_exc()}")
            return None
    
    try:
        respect_rate_limit(url)
    except Exception as e:
        logger.error(f"Error respecting rate limit for {url}: {e}\n{traceback.format_exc()}")
    
    try:
        response = session.get(url, headers=HEADERS, timeout=20, verify=True)
        response.raise_for_status()
        raw_content = response.content or b''
        logger.debug(f"HTTP status: {response.status_code}, Headers: {dict(response.headers)}, Content length: {len(raw_content)} bytes")
        
        content_hash = hashlib.sha256(raw_content or url.encode('utf-8')).hexdigest()
        logger.debug(f"Computed content_hash: {content_hash}")
        
        soup = None
        if 'text/html' in response.headers.get('Content-Type', ''):
            try:
                soup = BeautifulSoup(response.text, 'html.parser')
                title = extract_title(url, soup)
                summary = extract_summary(url, soup)
                tags = extract_tags(url, soup)
            except Exception as e:
                logger.error(f"Error parsing HTML for {url}: {e}\n{traceback.format_exc()}")
                title = extract_title(url)
                summary = extract_summary(url)
                tags = extract_tags(url)
        else:
            title = extract_title(url)
            summary = extract_summary(url)
            tags = extract_tags(url)
        
        new_urls = extract_urls(response, url, blacklist_check_endpoint, jwt_token)[:50]
        logger.info(f"Completed crawl for {url}: title='{title}', summary_len={len(summary)}, tags_count={len(tags.split(',')) if tags else 0}, content_hash={content_hash[:8]}..., new_urls={len(new_urls)} in {time.time() - start_time:.2f}s")
        return {
            'url': url[:2048],
            'title': title,
            'summary': summary,
            'tags': tags,
            'content_hash': content_hash,
            'new_urls': new_urls
        }
    except requests.exceptions.RequestException as e:
        logger.error(f"Request error for {url}: {e}\n{traceback.format_exc()}")
        log_queue.put(f"Request error for {url}: {e}")
        content_hash = hashlib.sha256(url.encode('utf-8')).hexdigest()
        logger.debug(f"Fallback content_hash: {content_hash}")
        title = extract_title(url)
        summary = extract_summary(url)
        tags = extract_tags(url)
        logger.info(f"Fallback crawl for {url}: title='{title}', summary_len={len(summary)}, tags_count={len(tags.split(',')) if tags else 0}, content_hash={content_hash[:8]}...")
        return {
            'url': url[:2048],
            'title': title,
            'summary': summary,
            'tags': tags,
            'content_hash': content_hash,
            'new_urls': []
        }
    except Exception as e:
        logger.error(f"Unexpected error crawling {url}: {e}\n{traceback.format_exc()}")
        log_queue.put(f"Unexpected error crawling {url}: {e}")
        return None

def fetch_urls(fetch_urls_endpoint, jwt_token, max_retries=3):
    """Fetch URLs to crawl from the server with exponential backoff."""
    try:
        start_time = time.time()
        headers = HEADERS.copy()
        if jwt_token:
            headers['Authorization'] = f'Bearer {jwt_token}'
        for attempt in range(max_retries):
            try:
                logger.info(f"Fetching URLs (attempt {attempt+1}/{max_retries})")
                response = session.get(fetch_urls_endpoint, headers=headers, timeout=10, verify=True)
                response.raise_for_status()
                data = response.json()
                urls = data.get('urls', [])
                logger.info(f"Fetched {len(urls)} URLs in {time.time() - start_time:.2f}s")
                if not urls and attempt == max_retries - 1:
                    logger.warning("No URLs available after retries; requesting reset")
                    try:
                        response = session.post(f'{fetch_urls_endpoint}/reset', headers=headers, timeout=10)
                        response.raise_for_status()
                        logger.info("Requested crawler reset")
                    except Exception as e:
                        logger.error(f"Failed to request reset: {e}\n{traceback.format_exc()}")
                return urls
            except requests.exceptions.RequestException as e:
                logger.error(f"Error fetching URLs: {e}\n{traceback.format_exc()}")
                log_queue.put(f"Error fetching URLs: {e}")
                if e.response and e.response.status_code == 401:
                    logger.error("Authentication failed; check JWT token")
                    log_queue.put("Authentication failed; check JWT token")
                    time.sleep(10)
                else:
                    time.sleep(2 ** (attempt + 1))
            except Exception as e:
                logger.error(f"Unexpected error fetching URLs: {e}\n{traceback.format_exc()}")
                log_queue.put(f"Unexpected error fetching URLs: {e}")
                time.sleep(2 ** (attempt + 1))
        logger.error(f"Failed to fetch URLs after retries in {time.time() - start_time:.2f}s")
        return []
    except Exception as e:
        logger.error(f"Error in fetch_urls: {e}\n{traceback.format_exc()}")
        return []

def submit_crawl_data(data, submit_data_endpoint, jwt_token, max_retries=3, processed_urls=None):
    """Submit crawled data to the server, checking for duplicates and validating data."""
    try:
        start_time = time.time()
        url = data.get('url')
        if url in processed_urls:
            logger.warning(f"Skipping duplicate submission for {url}")
            log_queue.put(f"Skipping duplicate submission for {url}")
            return False
        try:
            data['url'] = data.get('url', '')[:2048]
            data['title'] = sanitize_text(data.get('title') or 'Untitled')
            data['summary'] = sanitize_text(data.get('summary') or 'No summary available')
            data['tags'] = data.get('tags') or ','.join([f"web{i}" for i in range(20)])
            data['content_hash'] = data.get('content_hash') or hashlib.sha256(url.encode('utf-8')).hexdigest()
            data['new_urls'] = [u[:2048] for u in data.get('new_urls', [])[:50]]
            generic_tags = ','.join([f"web{i}" for i in range(20)])
            if not data['tags'] or data['tags'] == generic_tags:
                logger.error(f"Invalid data for {url}: title={data['title']}, summary_len={len(data['summary'])}, tags_count={len(data['tags'].split(',')) if data['tags'] else 0}, content_hash={data['content_hash'][:8]}...")
                return False
            logger.debug(f"Submitting data for {url}: {json.dumps(data, indent=2, ensure_ascii=False)}")
        except Exception as e:
            logger.error(f"Error validating data for {url}: {e}\n{traceback.format_exc()}")
            return False
        
        headers = HEADERS.copy()
        if jwt_token:
            headers['Authorization'] = f'Bearer {jwt_token}'
        for attempt in range(max_retries):
            try:
                logger.info(f"Submitting data for {url} (attempt {attempt+1}/{max_retries})")
                tag_count = len(data['tags'].split(',')) if data['tags'] else 0
                if tag_count < 20:
                    logger.warning(f"Data for {url} has only {tag_count} tags; proceeding with warning")
                    log_queue.put(f"Data for {url} has only {tag_count} tags; proceeding with warning")
                response = session.post(submit_data_endpoint, json=data, headers=headers, timeout=10, verify=True)
                response.raise_for_status()
                processed_urls.add(url)
                logger.info(f"Submitted data for {url} in {time.time() - start_time:.2f}s: {response.json().get('message')}")
                log_queue.put(f"Submitted data for {url}")
                return True
            except requests.exceptions.RequestException as e:
                logger.error(f"Error submitting data for {url}: {e}\n{traceback.format_exc()}")
                log_queue.put(f"Error submitting data for {url}: {e}")
                time.sleep(2 ** attempt)
            except Exception as e:
                logger.error(f"Unexpected error submitting data for {url}: {e}\n{traceback.format_exc()}")
                log_queue.put(f"Unexpected error submitting data for {url}: {e}")
                time.sleep(2 ** attempt)
        logger.error(f"Failed to submit data for {url} after retries in {time.time() - start_time:.2f}s")
        return False
    except Exception as e:
        logger.error(f"Error in submit_crawl_data for {url}: {e}\n{traceback.format_exc()}")
        return False

def worker_thread(stop_event, pause_event, api_base_url, jwt_token, enforce_robots):
    """Worker thread function with deduplication and robots.txt enforcement."""
    try:
        FETCH_URLS_ENDPOINT = f'{api_base_url}/urls'
        SUBMIT_DATA_ENDPOINT = f'{api_base_url}/submit'
        BLACKLIST_CHECK_ENDPOINT = f'{api_base_url}/blacklist_domain'
        logger.debug(f"Worker thread started with API base URL: {api_base_url}")
        processed_urls = set()
        process = psutil.Process()
        while not stop_event.is_set():
            if pause_event.is_set():
                time.sleep(1)
                continue
            try:
                start_time = time.time()
                memory_mb = process.memory_info().rss / (1024 * 1024)
                logger.info(f"Worker memory usage: {memory_mb:.2f} MB")
                urls = fetch_urls(FETCH_URLS_ENDPOINT, jwt_token)
                if not urls:
                    logger.info(f"No URLs to crawl, waiting... (worker cycle: {time.time() - start_time:.2f}s)")
                    log_queue.put("No URLs to crawl, waiting...")
                    time.sleep(random.uniform(5, 10))
                    continue
                
                for url in urls:
                    if stop_event.is_set() or pause_event.is_set():
                        break
                    if url in processed_urls:
                        logger.warning(f"Skipping already processed URL: {url}")
                        continue
                    data = crawl_url(url, BLACKLIST_CHECK_ENDPOINT, jwt_token, enforce_robots)
                    if data:
                        if submit_crawl_data(data, SUBMIT_DATA_ENDPOINT, jwt_token, processed_urls=processed_urls):
                            logger.info(f"Successfully processed {url} in {time.time() - start_time:.2f}s")
                        else:
                            logger.error(f"Failed to submit data for {url}")
                    else:
                        logger.info(f"Skipped crawling {url} in {time.time() - start_time:.2f}s")
                    time.sleep(random.uniform(0.5, 2))
                
            except Exception as e:
                logger.error(f"Worker error: {e}\n{traceback.format_exc()}")
                log_queue.put(f"Worker error: {e}")
                time.sleep(5)
    except Exception as e:
        logger.error(f"Error in worker_thread: {e}\n{traceback.format_exc()}")

def start_workers(num_threads, stop_event, pause_event, api_base_url=API_BASE_URL, jwt_token=None, enforce_robots=True):
    """Start the specified number of worker threads with robots.txt enforcement option."""
    try:
        num_threads = min(num_threads, MAX_THREADS)
        logger.info(f"Starting {num_threads} crawler worker threads (capped at {MAX_THREADS}) with API: {api_base_url}, enforce_robots: {enforce_robots}")
        log_queue.put(f"Starting {num_threads} crawler worker threads (capped at {MAX_THREADS}) with API: {api_base_url}")
        threads = []
        for i in range(num_threads):
            thread = threading.Thread(
                target=worker_thread,
                args=(stop_event, pause_event, api_base_url, jwt_token, enforce_robots),
                name=f"Worker-{i+1}",
                daemon=True
            )
            thread.start()
            threads.append(thread)
        return threads
    except Exception as e:
        logger.error(f"Error starting workers: {e}\n{traceback.format_exc()}")
        return []

if __name__ == '__main__':
    try:
        stop_event = threading.Event()
        pause_event = threading.Event()
        threads = start_workers(4, stop_event, pause_event)
        try:
            while True:
                time.sleep(1)
        except KeyboardInterrupt:
            logger.info("Shutting down workers")
            stop_event.set()
            for thread in threads:
                thread.join()
    except Exception as e:
        logger.error(f"Error in main: {e}\n{traceback.format_exc()}")
    finally:
        session.close()