---
name: ssl-proxy-troubleshoot-fb786c
description: Systematic approach to diagnosing and resolving SSL/proxy connectivity issues with restricted websites
---

# SSL/Proxy Connectivity Troubleshooting

This skill provides a structured workflow for diagnosing and resolving network connectivity issues when accessing government websites, corporate portals, or other restricted domains that commonly exhibit SSL certificate problems, proxy requirements, or access restrictions.

## When to Use

Apply this skill when:
- HTTP requests fail with SSL/TLS certificate errors
- Connections timeout or are refused through corporate/government networks
- Proxy settings may be interfering with connectivity
- Primary data sources are inaccessible and alternatives are needed

## Diagnostic Workflow

### Step 1: Initial Connectivity Assessment

Test basic connectivity before attempting complex solutions:

```bash
# Test DNS resolution
nslookup target-domain.gov
dig target-domain.gov

# Test basic TCP connectivity
timeout 5 bash -c 'cat < /dev/null > /dev/tcp/target-domain.gov/443' && echo "Port 443 open" || echo "Port 443 closed"

# Test with curl (verbose)
curl -v https://target-domain.gov 2>&1 | head -50
```

### Step 2: SSL Certificate Investigation

If SSL errors occur, diagnose the certificate issue:

```bash
# Check certificate details
openssl s_client -connect target-domain.gov:443 -servername target-domain.gov 2>/dev/null | openssl x509 -noout -dates -subject -issuer

# Test with different SSL versions
curl --tlsv1.2 -v https://target-domain.gov
curl --tlsv1.3 -v https://target-domain.gov

# Try disabling verification (testing only)
curl -k https://target-domain.gov
```

### Step 3: Proxy Configuration Testing

Test various proxy configurations:

```bash
# Try without proxy
curl --noproxy "*" https://target-domain.gov

# Try with system proxy
curl -x http://proxy.example.com:8080 https://target-domain.gov

# Try explicit no-proxy for .gov domains
export no_proxy=".gov,.mil,.edu"
curl https://target-domain.gov

# Test with different proxy protocols
curl -x http://proxy:8080 https://target-domain.gov
curl -x https://proxy:8080 https://target-domain.gov
curl -x socks5://proxy:1080 https://target-domain.gov
```

### Step 4: Protocol and Port Variations

Test alternative access methods:

```bash
# Try HTTP instead of HTTPS
curl http://target-domain.gov

# Try alternative ports
curl https://target-domain.gov:8443
curl https://target-domain.gov:4443

# Try www subdomain variation
curl https://www.target-domain.gov

# Try alternative domain extensions
curl https://target-domain.state.il.us
```

### Step 5: User-Agent and Header Manipulation

Some sites block automated access:

```bash
# Use browser user-agent
curl -A "Mozilla/5.0 (Windows NT 10.0; Win64; x64) AppleWebKit/537.36" https://target-domain.gov

# Add common headers
curl -H "Accept: text/html" -H "Accept-Language: en-US" https://target-domain.gov

# Include referer header
curl -e "https://www.google.com/" https://target-domain.gov
```

### Step 6: Python Requests Alternative

If curl fails, try Python with different configurations:

```python
import requests
from requests.adapters import HTTPAdapter

# Disable SSL verification (testing only)
session = requests.Session()
session.verify = False
session.mount('https://', HTTPAdapter())

try:
    response = session.get('https://target-domain.gov', timeout=30)
    print(f"Status: {response.status_code}")
except Exception as e:
    print(f"Error: {e}")

# Try with custom SSL context
import ssl
context = ssl.create_default_context()
context.check_hostname = False
context.verify_mode = ssl.CERT_NONE
```

## Fallback Strategies

When primary data sources remain inaccessible after exhausting troubleshooting:

### 1. Alternative Data Sources
- Search for mirror sites or archived versions (Wayback Machine)
- Check if data is available through state/federal data portals (data.gov)
- Look for API endpoints that may have different access requirements
- Search for downloadable datasets on alternative domains

### 2. Indirect Access Methods
- Check if the same data is available through partner organizations
- Look for cached versions through search engines
- Contact the organization directly for data access
- Check if data is available through FOIA requests or public records

### 3. Documentation and Reporting
When access fails after systematic troubleshooting:
```markdown
## Access Attempt Summary
- **Target**: [domain]
- **Methods Tried**: [list all approaches]
- **Errors Encountered**: [specific error messages]
- **Time Spent**: [duration]
- **Recommendation**: [alternative sources or next steps]
```

## Decision Tree

```
SSL Error?
├── Yes → Try curl -k (test only)
│         ├── Works → Document SSL issue, proceed with verification disabled
│         └── Fails → Continue diagnostics
│
Proxy Issue?
├── Suspected → Try --noproxy "*"
│               ├── Works → Configure no_proxy for target domain
│               └── Fails → Try explicit proxy settings
│
Timeout/Refused?
├── Yes → Try HTTP instead of HTTPS
│         Try alternative ports
│         Try www subdomain
│         └── All fail → Implement fallback strategies
│
All Methods Fail?
└── Implement fallback strategies and document attempts
```

## Important Notes

1. **Security Considerations**: Disabling SSL verification should only be used for diagnosis in trusted environments. Never use in production without understanding the risks.

2. **Rate Limiting**: Add delays between requests to avoid triggering rate limits or IP blocks.

3. **Documentation**: Always document which methods were tried and their results for future reference.

4. **Iteration Limit**: If 5+ distinct approaches fail, pivot to fallback strategies rather than continuing to iterate on the same blocked endpoint.

5. **Legal Compliance**: Ensure all access attempts comply with terms of service and applicable laws.

## Success Criteria

This troubleshooting workflow is complete when:
- [ ] Data successfully retrieved from primary source, OR
- [ ] All reasonable access methods exhausted (minimum 5 distinct approaches), OR
- [ ] Viable alternative data source identified and accessed, OR
- [ ] Clear documentation provided explaining access failure and recommended next steps