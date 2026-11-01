---
name: ssl-proxy-troubleshoot
description: Systematic workflow for troubleshooting SSL/proxy connectivity issues with government websites
---

# SSL/Proxy Troubleshooting for Government Websites

This skill provides a systematic approach to diagnosing and resolving SSL certificate and proxy connectivity issues when accessing government websites, which often have stricter security configurations than commercial sites.

## When to Use This Skill

- Cannot access a government or institutional website
- SSL certificate validation errors occur
- Proxy configuration blocks or interferes with requests
- Primary data sources are intermittently or persistently inaccessible

## Systematic Troubleshooting Workflow

### Step 1: Diagnose the Connection Issue

First, identify what type of connectivity problem you're facing:

```python
import requests
import ssl

url = "https://example.gov/data"

# Test basic connectivity
try:
    response = requests.get(url, timeout=10)
    print(f"Status: {response.status_code}")
except requests.exceptions.SSLError as e:
    print(f"SSL Error: {e}")
except requests.exceptions.ProxyError as e:
    print(f"Proxy Error: {e}")
except requests.exceptions.ConnectionError as e:
    print(f"Connection Error: {e}")
except Exception as e:
    print(f"Other Error: {e}")
```

### Step 2: Try Protocol Variations

Government sites sometimes have misconfigured redirects or support only specific protocols:

```python
urls_to_try = [
    "https://example.gov/data",
    "http://example.gov/data",  # Try unencrypted
    "https://www.example.gov/data",  # Try www subdomain
    "http://www.example.gov/data",
]

for url in urls_to_try:
    try:
        response = requests.get(url, timeout=10, verify=False)
        if response.status_code == 200:
            print(f"Success with: {url}")
            break
    except Exception as e:
        print(f"Failed {url}: {e}")
```

### Step 3: Disable SSL Verification (Temporary Debug)

Only use this for debugging. If it works, the issue is SSL certificate-related:

```python
# Option A: Disable verification entirely (debug only)
response = requests.get(url, verify=False, timeout=30)

# Option B: Use custom SSL context with relaxed settings
import ssl
ssl_context = ssl.create_default_context()
ssl_context.check_hostname = False
ssl_context.verify_mode = ssl.CERT_NONE

response = requests.get(url, verify=False, timeout=30)
```

**Warning:** Never use `verify=False` in production. If this is the only working option, document the SSL issue and seek alternative data sources.

### Step 4: Manipulate Proxy Settings

Government networks often require specific proxy configurations:

```python
# Option A: Disable proxy entirely
session = requests.Session()
session.trust_env = False  # Ignore environment proxy settings
response = session.get(url, timeout=30)

# Option B: Explicitly set proxy
proxies = {
    "http": "http://proxy.example.com:8080",
    "https": "http://proxy.example.com:8080",
}
response = requests.get(url, proxies=proxies, timeout=30, verify=False)

# Option C: Try without any proxy configuration
import os
os.environ.pop("HTTP_PROXY", None)
os.environ.pop("HTTPS_PROXY", None)
os.environ.pop("http_proxy", None)
os.environ.pop("https_proxy", None)
response = requests.get(url, timeout=30)
```

### Step 5: Test Alternative Domains and Endpoints

Government data is often mirrored or available through alternative access points:

```python
# Common alternative patterns for government sites
alternative_urls = [
    url.replace(".gov", ".gov.uk"),  # Regional variations
    url.replace("data.", "api."),  # API subdomain
    url.replace("https://", "https://archive."),  # Archive mirror
    url.replace("/data", "/download"),  # Alternative path
]

# Check for data.gov or similar portals
portal_search = f"site:data.gov {topic_keyword}"
```

### Step 6: Use Different HTTP Clients

Some clients handle SSL/proxy differently:

```python
# Try urllib instead of requests
import urllib.request
import ssl

ssl_context = ssl._create_unverified_context()
response = urllib.request.urlopen(url, context=ssl_context, timeout=30)

# Try curl via subprocess
import subprocess
result = subprocess.run(
    ["curl", "-k", "-L", "--max-time", "30", url],
    capture_output=True,
    text=True
)
```

## Fallback Strategies When Primary Sources Fail

When all troubleshooting attempts fail after 5-10 iterations:

### Strategy 1: Search for Alternative Data Sources

```python
# Search for the same data from different sources
search_queries = [
    f"{dataset_name} site:epa.gov",
    f"{dataset_name} site:data.gov",
    f"{dataset_name} state database",
    f"{dataset_name} public download",
]
```

### Strategy 2: Check for Archived Versions

```python
# Try Wayback Machine
wayback_url = f"https://web.archive.org/web/*/{original_url}"

# Check for cached versions
# Try Google Cache: http://webcache.googleusercontent.com/search?q=cache:{url}
```

### Strategy 3: Look for API Alternatives

Many government datasets have REST APIs even when web interfaces fail:

```python
# Common API patterns
api_endpoints = [
    url.replace("/portal", "/api"),
    url + "/api/v1/data",
    url + "/rest/data",
]
```

### Strategy 4: Contact or Document

If data is critical and unavailable:
1. Document all attempted URLs and error messages
2. Note the timeframe of access attempts
3. Search for contact information to report the issue
4. Consider if the task can proceed with partial or alternative data

## Decision Matrix

| Issue Type | Symptom | Recommended Action |
|------------|---------|-------------------|
| SSL Certificate | SSLError, CERT_VERIFY_FAILED | Try `verify=False` for debug, then find alternative source |
| Proxy Blocking | ProxyError, Connection refused | Set `trust_env=False`, try direct connection |
| Timeout | ReadTimeout, ConnectTimeout | Increase timeout, try different client |
| 403/401 Errors | HTTP status errors | Check for required headers, authentication |
| DNS Failure | Name resolution error | Try IP address directly, check alternative domains |

## Best Practices

1. **Limit iterations**: After 5-10 failed attempts with different approaches, pivot to fallback strategies
2. **Log everything**: Record which URLs, settings, and methods were tried
3. **Respect rate limits**: Government sites may throttle automated access
4. **Check robots.txt**: Verify automated access is permitted
5. **Prefer official APIs**: More stable than web scraping
6. **Document SSL issues**: If `verify=False` is required, flag this as a security concern

## Example: Complete Troubleshooting Function

```python
def robust_government_request(url, max_attempts=5):
    """Attempt to fetch government URL with multiple fallback strategies."""
    
    strategies = [
        {"verify": True, "trust_env": True, "timeout": 30},
        {"verify": False, "trust_env": True, "timeout": 30},
        {"verify": False, "trust_env": False, "timeout": 30},
        {"verify": False, "trust_env": False, "timeout": 60},
    ]
    
    for i, config in enumerate(strategies[:max_attempts]):
        try:
            session = requests.Session()
            session.trust_env = config["trust_env"]
            response = session.get(
                url,
                verify=config["verify"],
                timeout=config["timeout"]
            )
            if response.status_code == 200:
                return response
        except Exception as e:
            print(f"Attempt {i+1} failed: {e}")
            continue
    
    return None  # All strategies failed
```