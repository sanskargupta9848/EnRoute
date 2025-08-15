import json
import hashlib
from flask import Flask, jsonify, request
from functools import wraps
import jwt
import logging
from logging.handlers import RotatingFileHandler
import psycopg2
from psycopg2.extras import RealDictCursor
from urllib.parse import urlparse
import time
import threading
import queue
from contextlib import contextmanager
import psutil
import traceback
from crawler_config import DB_HOST, DB_PORT, DB_NAME, DB_USER, DB_PASS, JWT_SECRET

app = Flask(__name__)

# Configure logging
logging.basicConfig(
    level=logging.DEBUG,
    format='%(asctime)s [%(levelname)s] [%(threadName)s] %(message)s',
    handlers=[
        RotatingFileHandler('crawler_server.log', maxBytes=10*1024*1024, backupCount=5),
        logging.StreamHandler()
    ]
)
logger = logging.getLogger(__name__)

# Global state
blacklisted_domains = set()
db_pool = queue.Queue(maxsize=10)
dedupe_enabled = True
dedupe_interval = 5 * 60
last_dedupe_time = 0
current_domain = None

def get_db_config():
    """Return database configuration from crawler_config."""
    return {
        'dbname': DB_NAME,
        'user': DB_USER,
        'password': DB_PASS,
        'host': DB_HOST,
        'port': DB_PORT
    }

@contextmanager
def get_pg_connection():
    """Get a PostgreSQL connection with proper cleanup."""
    conn = None
    try:
        try:
            conn = db_pool.get(block=False)
            if conn.closed:
                conn = psycopg2.connect(**get_db_config())
            else:
                conn.rollback()
        except queue.Empty:
            conn = psycopg2.connect(**get_db_config())
        yield conn
    except Exception as e:
        logger.error(f"Database connection error: {e}\n{traceback.format_exc()}")
        if conn:
            conn.rollback()
        raise
    finally:
        if conn and not conn.closed:
            try:
                db_pool.put(conn)
            except Exception as e:
                logger.error(f"Error returning connection to pool: {e}")
                conn.close()
        elif conn:
            conn.close()

def load_blacklist():
    """Load blacklisted domains and patterns from the database."""
    global blacklisted_domains
    try:
        with get_pg_connection() as conn:
            cur = conn.cursor()
            try:
                cur.execute("SELECT domain FROM blacklisted_domains")
                blacklisted_domains = {row[0] for row in cur.fetchall()}
                logger.info(f"Loaded {len(blacklisted_domains)} blacklisted domains and patterns")
            except psycopg2.Error as e:
                logger.error(f"Error executing blacklist query: {e}")
                conn.rollback()
                raise
            conn.commit()
    except Exception as e:
        logger.error(f"Error loading blacklist: {e}\n{traceback.format_exc()}")
        raise

def init_db():
    """Initialize database tables."""
    try:
        with get_pg_connection() as conn:
            cur = conn.cursor()
            cur.execute("""
                SELECT EXISTS (
                    SELECT FROM information_schema.tables 
                    WHERE table_name = 'crawl_queue'
                )
            """)
            if not cur.fetchone()[0]:
                cur.execute("""
                    CREATE TABLE crawl_queue (
                        id SERIAL PRIMARY KEY,
                        url VARCHAR(2048) UNIQUE NOT NULL,
                        status TEXT DEFAULT 'pending',
                        last_crawled TIMESTAMP
                    )
                """)
                logger.info("Created crawl_queue table")
            
            cur.execute("""
                SELECT EXISTS (
                    SELECT FROM information_schema.tables 
                    WHERE table_name = 'webpages'
                )
            """)
            if not cur.fetchone()[0]:
                cur.execute("""
                    CREATE TABLE webpages (
                        id SERIAL PRIMARY KEY,
                        url VARCHAR(2048) UNIQUE NOT NULL,
                        title TEXT DEFAULT 'Untitled',
                        summary TEXT DEFAULT 'No summary available',
                        tags TEXT DEFAULT '',
                        content_hash TEXT DEFAULT '',
                        domain TEXT,
                        timestamp TIMESTAMP DEFAULT CURRENT_TIMESTAMP
                    )
                """)
                logger.info("Created webpages table")
            
            cur.execute("""
                SELECT EXISTS (
                    SELECT FROM information_schema.tables 
                    WHERE table_name = 'blacklisted_domains'
                )
            """)
            if not cur.fetchone()[0]:
                cur.execute("""
                    CREATE TABLE blacklisted_domains (
                        id SERIAL PRIMARY KEY,
                        domain VARCHAR(255) UNIQUE NOT NULL,
                        added_at TIMESTAMP DEFAULT CURRENT_TIMESTAMP
                    )
                """)
                logger.info("Created blacklisted_domains table")
                cur.execute("""
                    INSERT INTO blacklisted_domains (domain)
                    VALUES ('*.wikihow.com')
                    ON CONFLICT (domain) DO NOTHING
                """)
                logger.info("Inserted default blacklist pattern: *.wikihow.com")
            
            cur.execute("CREATE INDEX IF NOT EXISTS idx_webpages_url ON webpages(url)")
            cur.execute("CREATE INDEX IF NOT EXISTS idx_blacklisted_domains_domain ON blacklisted_domains(domain)")
            conn.commit()
            logger.info("Database tables initialized")
            load_blacklist()
    except Exception as e:
        logger.error(f"Database initialization error: {e}\n{traceback.format_exc()}")
        raise

def godmode_required(f):
    """Decorator to require godmode JWT token."""
    @wraps(f)
    def decorated_function(*args, **kwargs):
        logger.debug(f"Applying godmode_required to {f.__name__}")
        auth_header = request.headers.get('Authorization')
        if not auth_header:
            logger.warning("Authorization header missing")
            return jsonify({'error': 'Authorization header required'}), 401
        try:
            token_type, token = auth_header.split()
            if token_type.lower() != 'bearer':
                logger.warning("Invalid token type")
                return jsonify({'error': 'Bearer token required'}), 401
            payload = jwt.decode(token, JWT_SECRET, algorithms=['HS256'])
            if not payload.get('godmode'):
                logger.warning("Godmode access required")
                return jsonify({'error': 'Godmode access required'}), 403
            return f(payload, *args, **kwargs)
        except jwt.InvalidTokenError as e:
            logger.error(f"Invalid JWT token: {e}")
            return jsonify({'error': 'Invalid token'}), 401
        except Exception as e:
            logger.error(f"Authentication error: {e}\n{traceback.format_exc()}")
            return jsonify({'error': str(e)}), 500
    return decorated_function

def deduplicate_urls():
    """Remove duplicate URLs from crawl_queue."""
    global last_dedupe_time
    while True:
        try:
            if dedupe_enabled and (time.time() - last_dedupe_time) >= dedupe_interval:
                with get_pg_connection() as conn:
                    cur = conn.cursor()
                    try:
                        cur.execute("""
                            WITH duplicates AS (
                                SELECT id, url, ROW_NUMBER() OVER (PARTITION BY url ORDER BY id) AS rn
                                FROM crawl_queue
                                WHERE status = 'pending'
                            )
                            DELETE FROM crawl_queue
                            WHERE id IN (SELECT id FROM duplicates WHERE rn > 1)
                        """)
                        deleted_count = cur.rowcount
                        if deleted_count > 0:
                            logger.info(f"Deduplicated {deleted_count} URLs")
                        conn.commit()
                    except psycopg2.Error as e:
                        logger.error(f"Deduplication query error: {e}")
                        conn.rollback()
        except Exception as e:
            logger.error(f"Deduplication error: {e}\n{traceback.format_exc()}")
            time.sleep(60)
        time.sleep(60)

def log_resources():
    """Log server resource usage periodically."""
    while True:
        process = psutil.Process()
        memory_mb = process.memory_info().rss / (1024 * 1024)
        cpu_percent = psutil.cpu_percent(interval=1)
        logger.info(f"Server resource usage: Memory={memory_mb:.2f} MB, CPU={cpu_percent:.2f}%")
        time.sleep(300)

@app.route('/api/crawler/status', methods=['GET', 'OPTIONS'], endpoint='get_status')
@godmode_required
def get_status(current_user):
    """Get crawler status."""
    logger.debug("Registering endpoint: get_status")
    if request.method == 'OPTIONS':
        return '', 200
    try:
        with get_pg_connection() as conn:
            cur = conn.cursor()
            try:
                cur.execute("SELECT COUNT(*) FROM crawl_queue WHERE status = 'pending'")
                pending_urls = cur.fetchone()[0]
                cur.execute("SELECT COUNT(*) FROM crawl_queue WHERE status = 'processing'")
                processing_urls = cur.fetchone()[0]
                cur.execute("SELECT COUNT(*) FROM crawl_queue WHERE status = 'completed'")
                crawled_urls = cur.fetchone()[0]
                logger.info(f"Status: {pending_urls} pending, {processing_urls} processing, {crawled_urls} crawled")
                conn.commit()
                return jsonify({
                    'pending_urls': pending_urls,
                    'processing_urls': processing_urls,
                    'crawled_urls': crawled_urls,
                    'current_domain': current_domain
                }), 200
            except psycopg2.Error as e:
                logger.error(f"Status query error: {e}")
                conn.rollback()
                raise
    except Exception as e:
        logger.error(f"Error fetching status: {e}\n{traceback.format_exc()}")
        return jsonify({'error': str(e)}), 500

@app.route('/api/crawler/config', methods=['POST', 'OPTIONS'], endpoint='update_config')
@godmode_required
def update_config(current_user):
    """Update crawler configuration."""
    logger.debug("Registering endpoint: update_config")
    if request.method == 'OPTIONS':
        return '', 200
    global dedupe_enabled, dedupe_interval
    try:
        data = request.get_json()
        dedupe_enabled = data.get('dedupe_enabled', dedupe_enabled)
        dedupe_interval = data.get('dedupe_interval', dedupe_interval)
        logger.info(f"Updated config: dedupe_enabled={dedupe_enabled}, dedupe_interval={dedupe_interval}")
        return jsonify({'message': 'Configuration updated'}), 200
    except Exception as e:
        logger.error(f"Error updating config: {e}\n{traceback.format_exc()}")
        return jsonify({'error': str(e)}), 500

@app.route('/api/crawler/urls', methods=['GET', 'POST', 'OPTIONS'], endpoint='manage_urls')
@godmode_required
def manage_urls(current_user):
    """Fetch URLs to crawl or reset the queue."""
    logger.debug("Registering endpoint: manage_urls")
    global current_domain
    if request.method == 'OPTIONS':
        return '', 200
    try:
        with get_pg_connection() as conn:
            cur = conn.cursor(cursor_factory=RealDictCursor)
            try:
                if request.method == 'POST' and request.get_json().get('reset'):
                    current_domain = None
                    cur.execute("UPDATE crawl_queue SET status = 'pending' WHERE status = 'processing'")
                    cur.execute("DELETE FROM crawl_queue WHERE status = 'completed'")
                    conn.commit()
                    logger.info("Reset crawl queue")
                    return jsonify({'message': 'Queue reset'}), 200
                else:
                    # Fetch blacklist patterns
                    cur.execute("SELECT domain FROM blacklisted_domains")
                    blacklist_patterns = [row['domain'] for row in cur.fetchall() if row.get('domain')]
                    logger.debug(f"Fetched {len(blacklist_patterns)} blacklist patterns")
                    if not blacklist_patterns:
                        logger.info("No blacklist patterns found; proceeding without filtering")
                        blacklist_patterns = []
                    
                    query_conditions = []
                    query_params = []
                    for pattern in blacklist_patterns:
                        if pattern.startswith('*.'):
                            query_conditions.append("url NOT LIKE %s")
                            query_params.append(f'%://{pattern[2:]}%')
                        else:
                            query_conditions.append("url NOT LIKE %s")
                            query_params.append(f'%://{pattern}%')
                    where_clause = " AND ".join(query_conditions) if query_conditions else "TRUE"
                    query = f"""
                        UPDATE crawl_queue
                        SET status = 'processing'
                        WHERE id IN (
                            SELECT id
                            FROM crawl_queue
                            WHERE status = 'pending'
                            AND ({where_clause})
                            ORDER BY id
                            LIMIT 100
                            FOR UPDATE SKIP LOCKED
                        )
                        RETURNING url
                    """
                    cur.execute(query, query_params)
                    urls = [row['url'] for row in cur.fetchall()]
                    if not urls:
                        logger.info("No pending URLs available")
                        return jsonify({'urls': []}), 200
                    domain = urlparse(urls[0]).netloc
                    current_domain = domain
                    filtered_urls = [url for url in urls if urlparse(url).netloc == domain]
                    conn.commit()
                    logger.info(f"Returning {len(filtered_urls)} URLs for domain {domain}")
                    return jsonify({'urls': filtered_urls}), 200
            except psycopg2.Error as e:
                logger.error(f"Manage URLs query error: {e}")
                conn.rollback()
                raise
    except Exception as e:
        logger.error(f"Error managing URLs: {e}\n{traceback.format_exc()}")
        return jsonify({'error': str(e)}), 500

@app.route('/api/crawler/submit', methods=['POST', 'OPTIONS'], endpoint='submit_crawl_data')
def submit_crawl_data():
    """Submit crawl data from worker nodes."""
    logger.debug("Registering endpoint: submit_crawl_data")
    if request.method == 'OPTIONS':
        return '', 200
    try:
        with get_pg_connection() as conn:
            data = request.get_json()
            logger.debug(f"Received data: {json.dumps(data, indent=2, ensure_ascii=False)}")
            url = data.get('url')
            if not url:
                logger.error("URL is missing in submitted data")
                return jsonify({'message': 'URL is required'}), 400
            
            # Truncate URL if necessary
            url = url[:2048]
            title = (data.get('title') or 'Untitled')[:255]
            summary = (data.get('summary') or 'No summary available')[:2000]
            tags = data.get('tags') or ''
            content_hash = data.get('content_hash') or hashlib.sha256(url.encode('utf-8')).hexdigest()
            domain = urlparse(url).netloc
            new_urls = data.get('new_urls', [])[:50]
            
            # Validate data
            generic_tags = ','.join([f"web{i}" for i in range(20)])
            if not tags or tags == generic_tags:
                logger.error(f"Invalid data for {url}: title={title}, summary_len={len(summary)}, tags_count={len(tags.split(',')) if tags else 0}, content_hash={content_hash[:8]}...")
                return jsonify({'message': 'Non-empty, non-generic tags required'}), 400
            tag_count = len(tags.split(',')) if tags else 0
            if tag_count < 20:
                logger.warning(f"URL {url} has only {tag_count} tags; accepting with warning")
            cur = conn.cursor()
            cur.execute("SELECT domain FROM blacklisted_domains")
            blacklist_patterns = [row[0] for row in cur.fetchall()]
            for pattern in blacklist_patterns:
                if pattern.startswith('*.'):
                    if domain.endswith(pattern[2:]):
                        logger.info(f"Rejected submission for {url}; domain {domain} matches blacklist pattern {pattern}")
                        return jsonify({'message': f'Domain {domain} is blacklisted'}), 400
                elif domain == pattern:
                    logger.info(f"Rejected submission for {url}; domain {domain} is blacklisted")
                    return jsonify({'message': f'Domain {domain} is blacklisted'}), 400
            
            try:
                # Delete related rows in tags and images to avoid foreign key issues
                cur.execute("DELETE FROM tags WHERE url = %s", (url,))
                cur.execute("DELETE FROM images WHERE url = %s", (url,))
                
                # Insert or update webpage record
                cur.execute("""
                    INSERT INTO webpages (url, title, summary, tags, content_hash, domain)
                    VALUES (%s, %s, %s, %s, %s, %s)
                    ON CONFLICT (url) DO UPDATE 
                    SET title = COALESCE(EXCLUDED.title, webpages.title),
                        summary = COALESCE(EXCLUDED.summary, webpages.summary),
                        tags = COALESCE(EXCLUDED.tags, webpages.tags),
                        content_hash = COALESCE(EXCLUDED.content_hash, webpages.content_hash),
                        timestamp = CURRENT_TIMESTAMP,
                        domain = COALESCE(EXCLUDED.domain, webpages.domain)
                    RETURNING id
                """, (url, title, summary, tags, content_hash, domain))
                webpage_id = cur.fetchone()[0]
                logger.info(f"{'Updated' if cur.rowcount else 'Inserted'} webpage record for {url} (id: {webpage_id}), title={title}, summary_len={len(summary)}, tags_count={tag_count}, content_hash={content_hash[:8]}...")
                
                # Update crawl_queue status
                cur.execute("""
                    UPDATE crawl_queue 
                    SET status = 'completed', 
                        last_crawled = CURRENT_TIMESTAMP 
                    WHERE url = %s
                """, (url,))
                
                # Insert new URLs
                for new_url in new_urls:
                    new_url = new_url[:2048]
                    new_domain = urlparse(new_url).netloc
                    skip = False
                    for pattern in blacklist_patterns:
                        if pattern.startswith('*.'):
                            if new_domain.endswith(pattern[2:]):
                                skip = True
                                break
                        elif new_domain == pattern:
                            skip = True
                            break
                    if skip:
                        logger.info(f"Skipped adding {new_url[:50]}... to crawl_queue; domain {new_domain} matches blacklist pattern")
                        continue
                    try:
                        cur.execute("""
                            INSERT INTO crawl_queue (url)
                            VALUES (%s)
                            ON CONFLICT (url) DO NOTHING
                        """, (new_url,))
                        cur.execute("""
                            INSERT INTO webpages (url, domain)
                            VALUES (%s, %s)
                            ON CONFLICT (url) DO NOTHING
                        """, (new_url, new_domain))
                    except Exception as e:
                        logger.error(f"Error inserting new URL {new_url[:50]}...: {e}\n{traceback.format_exc()}")
                        continue
                conn.commit()
                logger.info(f"Processed {url} with {len(new_urls)} new URLs")
                return jsonify({'message': 'Data saved successfully'}), 200
            except psycopg2.Error as e:
                logger.error(f"Submit query error for {url}: {e}")
                conn.rollback()
                raise
    except psycopg2.Error as e:
        logger.error(f"Database error submitting crawl data for {url}: {e}\n{traceback.format_exc()}")
        return jsonify({'error': f'Database error: {str(e)}'}), 500
    except Exception as e:
        logger.error(f"Error submitting crawl data for {url}: {e}\n{traceback.format_exc()}")
        return jsonify({'error': str(e)}), 500

@app.route('/api/crawler/skip_domain', methods=['POST', 'OPTIONS'], endpoint='skip_domain')
@godmode_required
def skip_domain(current_user):
    """Skip the current domain being crawled."""
    logger.debug("Registering endpoint: skip_domain")
    if request.method == 'OPTIONS':
        return '', 200
    global current_domain
    if not current_domain:
        logger.info("No current domain to skip")
        return jsonify({'message': 'No current domain'}), 400
    try:
        with get_pg_connection() as conn:
            cur = conn.cursor()
            try:
                cur.execute("""
                    UPDATE crawl_queue 
                    SET status = 'completed' 
                    WHERE url LIKE %s AND status = 'processing'
                """, (f'%://{current_domain}%',))
                skipped_count = cur.rowcount
                conn.commit()
                logger.info(f"Skipped {skipped_count} URLs for domain {current_domain}")
                current_domain = None
                return jsonify({'message': f'Skipped domain {current_domain}'}), 200
            except psycopg2.Error as e:
                logger.error(f"Skip domain query error: {e}")
                conn.rollback()
                raise
    except Exception as e:
        logger.error(f"Error skipping domain {current_domain}: {e}\n{traceback.format_exc()}")
        return jsonify({'error': str(e)}), 500

@app.route('/api/crawler/blacklist', methods=['GET', 'OPTIONS'], endpoint='get_blacklist')
@godmode_required
def get_blacklist(current_user):
    """Get the list of blacklisted domains."""
    logger.debug("Registering endpoint: get_blacklist")
    if request.method == 'OPTIONS':
        return '', 200
    try:
        with get_pg_connection() as conn:
            cur = conn.cursor()
            try:
                cur.execute("SELECT domain FROM blacklisted_domains")
                domains = [row[0] for row in cur.fetchall()]
                logger.info(f"Returning {len(domains)} blacklisted domains")
                conn.commit()
                return jsonify({'domains': domains}), 200
            except psycopg2.Error as e:
                logger.error(f"Blacklist query error: {e}")
                conn.rollback()
                raise
    except Exception as e:
        logger.error(f"Error fetching blacklist: {e}\n{traceback.format_exc()}")
        return jsonify({'error': str(e)}), 500

@app.route('/api/crawler/blacklist_domain', methods=['GET', 'POST', 'OPTIONS'], endpoint='blacklist_domain')
@godmode_required
def blacklist_domain(current_user):
    """Add a domain to the blacklist or check if a domain is blacklisted."""
    logger.debug("Registering endpoint: blacklist_domain")
    if request.method == 'OPTIONS':
        return '', 200
    try:
        with get_pg_connection() as conn:
            cur = conn.cursor()
            if request.method == 'GET':
                domain = request.args.get('domain')
                if not domain:
                    logger.warning("Domain parameter missing for blacklist check")
                    return jsonify({'message': 'Domain is required'}), 400
                try:
                    cur.execute("SELECT domain FROM blacklisted_domains")
                    blacklist_patterns = [row[0] for row in cur.fetchall()]
                    is_blacklisted = False
                    for pattern in blacklist_patterns:
                        if pattern.startswith('*.'):
                            if domain.endswith(pattern[2:]):
                                is_blacklisted = True
                                break
                        elif domain == pattern:
                            is_blacklisted = True
                            break
                    logger.info(f"Returning blacklist status for domain: {domain} - {is_blacklisted}")
                    conn.commit()
                    return jsonify({'blacklisted': is_blacklisted}), 200
                except psycopg2.Error as e:
                    logger.error(f"Blacklist check query error: {e}")
                    conn.rollback()
                    raise
            data = request.get_json()
            domain = data.get('domain')
            if not domain:
                logger.warning("Domain is required for blacklisting")
                return jsonify({'message': 'Domain is required'}), 400
            try:
                cur.execute("""
                    INSERT INTO blacklisted_domains (domain)
                    VALUES (%s)
                    ON CONFLICT (domain) DO NOTHING
                """, (domain,))
                blacklisted_domains.add(domain)
                pattern = f'%://{domain[2:]}%' if domain.startswith('*.') else f'%://{domain}%'
                cur.execute("""
                    UPDATE crawl_queue 
                    SET status = 'blacklisted' 
                    WHERE url LIKE %s
                """, (pattern,))
                updated_rows = cur.rowcount
                cur.execute("""
                    DELETE FROM crawl_queue 
                    WHERE url LIKE %s
                """, (pattern,))
                deleted_rows = cur.rowcount
                cur.execute("""
                    DELETE FROM webpages 
                    WHERE domain LIKE %s
                """, (pattern[4:-1],))
                deleted_webpages = cur.rowcount
                conn.commit()
                logger.info(f"Blacklisted domain {domain}: marked {updated_rows} URLs, deleted {deleted_rows} from crawl_queue, {deleted_webpages} from webpages")
                return jsonify({'message': f'Domain {domain} blacklisted'}), 200
            except psycopg2.Error as e:
                logger.error(f"Blacklist domain query error: {e}")
                conn.rollback()
                raise
    except Exception as e:
        logger.error(f"Error blacklisting domain {domain}: {e}\n{traceback.format_exc()}")
        return jsonify({'error': str(e)}), 500

@app.route('/api/crawler/unblacklist_domain', methods=['POST', 'OPTIONS'], endpoint='unblacklist_domain')
@godmode_required
def unblacklist_domain(current_user):
    """Remove a domain from the blacklist."""
    logger.debug("Registering endpoint: unblacklist_domain")
    if request.method == 'OPTIONS':
        return '', 200
    data = request.get_json()
    domain = data.get('domain')
    if not domain:
        logger.warning("Domain is required for unblacklisting")
        return jsonify({'message': 'Domain is required'}), 400
    try:
        with get_pg_connection() as conn:
            cur = conn.cursor()
            try:
                cur.execute("DELETE FROM blacklisted_domains WHERE domain = %s", (domain,))
                if domain in blacklisted_domains:
                    blacklisted_domains.remove(domain)
                    logger.info(f"Unblacklisted domain {domain}")
                    conn.commit()
                    return jsonify({'message': f'Domain {domain} unblacklisted'}), 200
                logger.info(f"Domain {domain} not in blacklist")
                conn.commit()
                return jsonify({'message': f'Domain {domain} not in blacklist'}), 200
            except psycopg2.Error as e:
                logger.error(f"Unblacklist query error: {e}")
                conn.rollback()
                raise
    except Exception as e:
        logger.error(f"Error unblacklisting domain {domain}: {e}\n{traceback.format_exc()}")
        return jsonify({'error': str(e)}), 500

@app.route('/api/crawler/clear_blacklisted_urls', methods=['POST', 'OPTIONS'], endpoint='clear_blacklisted_urls')
@godmode_required
def clear_blacklisted_urls(current_user):
    """Clear URLs for a blacklisted domain from crawl_queue."""
    logger.debug("Registering endpoint: clear_blacklisted_urls")
    if request.method == 'OPTIONS':
        return '', 200
    data = request.get_json()
    domain = data.get('domain')
    if not domain:
        logger.warning("Domain is required for clearing blacklisted URLs")
        return jsonify({'message': 'Domain is required'}), 400
    try:
        with get_pg_connection() as conn:
            cur = conn.cursor()
            try:
                pattern = f'%://{domain[2:]}%' if domain.startswith('*.') else f'%://{domain}%'
                cur.execute("""
                    DELETE FROM crawl_queue 
                    WHERE url LIKE %s
                """, (pattern,))
                deleted_rows = cur.rowcount
                conn.commit()
                logger.info(f"Deleted {deleted_rows} URLs for domain {domain} from crawl_queue")
                return jsonify({'message': f'Cleared {deleted_rows} URLs for domain {domain}'}), 200
            except psycopg2.Error as e:
                logger.error(f"Clear blacklisted URLs query error: {e}")
                conn.rollback()
                raise
    except Exception as e:
        logger.error(f"Error clearing blacklisted URLs for {domain}: {e}\n{traceback.format_exc()}")
        return jsonify({'error': str(e)}), 500

@app.errorhandler(Exception)
def handle_error(error):
    logger.error(f"Server error: {str(error)}\n{traceback.format_exc()}")
    return jsonify({'error': str(error)}), 500

if __name__ == '__main__':
    init_db()
    threading.Thread(target=deduplicate_urls, daemon=True).start()
    threading.Thread(target=log_resources, daemon=True).start()