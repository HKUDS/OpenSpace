---
name: ssl-proxy-debug-workflow
description: Systematic debugging workflow for SSL/proxy connectivity issues with government and institutional websites
---

# SSL/Proxy Debugging Workflow for Government Websites

This skill provides a systematic approach to troubleshoot and resolve SSL certificate, proxy, and connectivity issues commonly encountered when accessing government and institutional websites.

## Recognizing the Problem

Typical symptoms indicating SSL/proxy issues:
- `requests.exceptions.SSLError` or `certificate verify failed` errors
- Connection timeouts despite the site being publicly accessible
- Proxy authentication failures
- HTTP 403/407 errors on otherwise valid requests
- Mixed success across different access methods (browser works, script fails)

## Systematic Troubleshooting Steps

### Step 1: Verify Basic Connectivity

Before assuming SSL issues, confirm the target is reachable:

```python
import requests
import socket

# Test DNS resolution
try:
    ip = socket.gethostbyname('example.gov')
    print(f"DNS resolved: {ip}")
except Exception as e:
    print(f"DNS failure: {e}")

# Test basic TCP connectivity
try:
    sock = socket.create_connection(('example.gov', 443), timeout=5)
    sock.close()
    print("TCP connection successful")
except Exception as e:
    print(f"TCP failure: {e}")
```

### Step 2: Try Protocol Variations

Test both HTTP and HTTPS, and try without www prefix:

```python
urls_to_try = [
    'https://example.gov',
    'http://example.gov',
    'https://www.example.gov',
    'http://www.example.gov',
    'https://subdomain.example.gov',
]

for url in urls_to_try:
    try:
        response = requests.get(url, timeout=10)
        print(f"SUCCESS: {url} - Status: {response.status_code}")
        break
    except Exception as e:
        print(f"FAILED: {url} - {type(e).__name__}: {e}")
```

### Step 3: Disable SSL Verification (Temporary Debug)

For debugging only - never use in production with sensitive data:

```python
import urllib3
urllib3.disable_warnings(urllib3.exceptions.InsecureRequestWarning)

response = requests.get('https://example.gov', verify=False, timeout=10)
print(f"Status: {response.status_code}")
```

### Step 4: Manipulate Proxy Settings

Government networks often require specific proxy configurations:

```python
# Try without proxy
session = requests.Session()
session.trust_env = False  # Ignore system proxy settings
response = session.get('https://example.gov', timeout=10)

# Try with explicit proxy
proxies = {
    'http': 'http://proxy.example.com:8080',
    'https': 'http://proxy.example.com:8080',
}
response = requests.get('https://example.gov', proxies=proxies, timeout=10)

# Try with proxy authentication
proxies = {
    'https': 'http://username:password@proxy.example.com:8080',
}
```

### Step 5: Adjust Request Headers

Some government sites block automated requests:

```python
headers = {
    'User-Agent': 'Mozilla/5.0 (Windows NT 10.0; Win64; x64) AppleWebKit/537.36',
    'Accept': 'text/html,application/xhtml+xml,application/xml;q=0.9,*/*;q=0.8',
    'Accept-Language': 'en-US,en;q=0.5',
    'Accept-Encoding': 'gzip, deflate',
    'Connection': 'keep-alive',
}
response = requests.get('https://example.gov', headers=headers, timeout=10)
```

### Step 6: Test Alternative Domains/Endpoints

Government data may be available through multiple portals:

```python
# Common alternative patterns
alternative_domains = [
    'data.example.gov',
    'api.example.gov',
    'services.example.gov',
    'example.illinois.gov',  # State-specific
    'www.epa.gov/example',   # Federal parent site
]

# Search for data mirrors
# Check data.gov, state open data portals, etc.
```

### Step 7: Increase Timeouts and Add Retries

Government sites may be slow or rate-limit:

```python
from requests.adapters import HTTPAdapter
from urllib3.util.retry import Retry

session = requests.Session()
retry = Retry(
    total=5,
    backoff_factor=2,
    status_forcelist=[429, 500, 502, 503, 504],
)
adapter = HTTPAdapter(max_retries=retry)
session.mount('https://', adapter)
session.mount('http://', adapter)

response = session.get('https://example.gov', timeout=30)
```

## Fallback Strategies When Primary Source Fails

### Strategy A: Find Alternative Data Sources

```python
# 1. Check federal aggregators
#    - data.gov
#    - epa.gov (for environmental data)
#    - census.gov (for demographic data)

# 2. Check state open data portals
#    - Format: data.{state}.gov or {state}.gov/open-data

# 3. Check county/municipal portals
#    - Often have more accessible APIs

# 4. Search for cached/archived versions
#    - Web Archive (archive.org)
#    - Google Cache
```

### Strategy B: Use Search to Discover Working Endpoints

```python
# Search for the data with specific file types
search_queries = [
    'site:example.gov well data filetype:csv',
    'site:example.gov water quality filetype:json',
    'example.gov API endpoint documentation',
]
```

### Strategy C: Manual Download as Last Resort

If programmatic access consistently fails:
1. Document the exact URL that works in a browser
2. Note any authentication/cookie requirements
3. Consider browser automation (Selenium/Playwright) as fallback
4. Schedule manual data collection if volume permits

## Complete Troubleshooting Function

```python
def debug_government_url(base_url, max_attempts=5):
    """Systematic debugging for government website access."""
    import requests
    import urllib3
    from requests.adapters import HTTPAdapter
    from urllib3.util.retry import Retry
    
    urllib3.disable_warnings(urllib3.exceptions.InsecureRequestWarning)
    
    # Generate URL variations
    variations = []
    for proto in ['https', 'http']:
        for prefix in ['', 'www.']:
            variations.append(f"{proto}://{prefix}{base_url}")
    
    # Configure retry session
    session = requests.Session()
    session.trust_env = False  # Bypass system proxy
    retry = Retry(total=3, backoff_factor=1, status_forcelist=[500, 502, 503, 504])
    adapter = HTTPAdapter(max_retries=retry)
    session.mount('https://', adapter)
    
    headers = {'User-Agent': 'Mozilla/5.0'}
    
    for url in variations:
        print(f"Trying: {url}")
        try:
            # First attempt: normal
            resp = session.get(url, headers=headers, timeout=15)
            if resp.status_code < 400:
                print(f"SUCCESS (normal): {url}")
                return {'url': url, 'method': 'normal', 'response': resp}
        except Exception as e:
            print(f"  Normal failed: {e}")
        
        try:
            # Second attempt: no SSL verify
            resp = session.get(url, headers=headers, verify=False, timeout=15)
            if resp.status_code < 400:
                print(f"SUCCESS (no-verify): {url}")
                return {'url': url, 'method': 'no-verify', 'response': resp}
        except Exception as e:
            print(f"  No-verify failed: {e}")
    
    return {'error': 'All attempts failed', 'url': base_url}
```

## Best Practices

1. **Never hardcode credentials** - Use environment variables for proxy auth
2. **Log all attempts** - Document which methods succeeded/failed for future reference
3. **Respect rate limits** - Add delays between requests (1-2 seconds minimum)
4. **Check robots.txt** - Verify scraping is permitted
5. **Have exit criteria** - Know when to abandon a source and find alternatives
6. **Cache successful configurations** - Save working URL/method combinations

## When to Give Up

After exhausting these steps, consider:
- The data may no longer be publicly available
- The site may require special authentication (contact the agency)
- Alternative sources may have the same data in more accessible format
- The task scope may need adjustment based on data availability