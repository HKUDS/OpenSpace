import os
import json
from typing import Dict, Any
from openspace.utils.logging import Logger

logger = Logger.get_logger(__name__)

def get_local_server_config() -> Dict[str, Any]:
    """
    Read local server configuration.
    
    Priority:
    1. Environment variable LOCAL_SERVER_URL (parsed into host/port)
    2. Config file local_server/config.json
    3. Defaults (127.0.0.1:5000)
    
    Returns:
        Dict with 'host' and 'port' from server config
    """
    # Check environment variable first (for OSWorld/remote VM integration)
    env_url = os.getenv("LOCAL_SERVER_URL")
    if env_url:
        try:
            # Parse URL like "http://localhost:5000"
            from urllib.parse import urlparse
            parsed = urlparse(env_url)
            host = parsed.hostname or '127.0.0.1'
            port = parsed.port or 5000
            logger.debug(f"Using LOCAL_SERVER_URL: {host}:{port}")
            return {
                'host': host,
                'port': port,
                'debug': False,
            }
        except Exception as e:
            logger.warning(f"Failed to parse LOCAL_SERVER_URL: {e}")
    
    # Find local_server config file
    try:
        # Try relative path from this file
        current_dir = os.path.dirname(__file__)
        config_path = os.path.join(current_dir, '../local_server/config.json')
        config_path = os.path.abspath(config_path)
        
        if os.path.exists(config_path):
            with open(config_path, 'r') as f:
                config = json.load(f)
                server_config = config.get('server', {})
                return {
                    'host': server_config.get('host', '127.0.0.1'),
                    'port': server_config.get('port', 5000),
                    'debug': server_config.get('debug', False),
                }
    except Exception as e:
        logger.debug(f"Failed to read local server config: {e}")
    
    # Return defaults
    return {
        'host': '127.0.0.1',
        'port': 5000,
        'debug': False,
    }


def get_client_base_url() -> str:
    """
    Get base URL for connecting to local server.
    
    Priority:
    1. Environment variable LOCAL_SERVER_URL
    2. Read from local_server/config.json
    3. Default http://localhost:5000
    
    Returns:
        Base URL string
    """
    # Check environment variable first
    env_url = os.getenv("LOCAL_SERVER_URL")
    if env_url:
        return env_url
    
    # Read from config file
    config = get_local_server_config()
    host = config['host']
    port = config['port']
    
    # Convert 0.0.0.0 to localhost for client
    if host == '0.0.0.0':
        host = 'localhost'
    
    return f"http://{host}:{port}"