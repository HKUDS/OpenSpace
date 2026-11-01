import platform
import subprocess
import tempfile
from typing import Dict, Any

from openspace.utils.logging import Logger
logger = Logger.get_logger(__name__)

platform_name = platform.system()


class FeatureChecker:
    def __init__(self, platform_adapter=None, accessibility_helper=None):
        self.platform_adapter = platform_adapter
        self.accessibility_helper = accessibility_helper
        self.platform = platform_name
        self._cache = {} 
    
    def check_screenshot_available(self, use_cache: bool = True) -> bool:
        if use_cache and 'screenshot' in self._cache:
            return self._cache['screenshot']
        
        try:
            import pyautogui
            from PIL import Image
            
            size = pyautogui.size()
            result = size.width > 0 and size.height > 0
            
            self._cache['screenshot'] = result
            logger.info(f"Screenshot check: {'available' if result else 'unavailable'}")
            return result
            
        except ImportError as e:
            logger.warning(f"Screenshot unavailable - missing dependency: {e}")
            self._cache['screenshot'] = False
            return False
        except Exception as e:
            logger.error(f"Screenshot check failed: {e}")
            self._cache['screenshot'] = False
            return False
    
    def check_shell_available(self, use_cache: bool = True) -> bool:
        if use_cache and 'shell' in self._cache:
            return self._cache['shell']
        
        try:
            if self.platform == "Windows":
                cmd = ['cmd', '/c', 'echo', 'test']
            else:
                cmd = ['echo', 'test']
            
            result = subprocess.run(
                cmd,
                capture_output=True,
                timeout=2,
                text=True
            )
            
            available = result.returncode == 0
            self._cache['shell'] = available
            logger.info(f"Shell check: {'available' if available else 'unavailable'}")
            return available
            
        except FileNotFoundError as e:
            logger.warning(f"Shell check failed - command not found: {e}")
            self._cache['shell'] = False
            return False
        except Exception as e:
            logger.error(f"Shell check failed: {e}")
            self._cache['shell'] = False
            return False
    
    def check_python_available(self, use_cache: bool = True) -> bool:
        if use_cache and 'python' in self._cache:
            return self._cache['python']
        
        python_commands = []
        if self.platform == "Windows":
            python_commands = ['py', 'python', 'python3']
        else:
            python_commands = ['python3', 'python']
        
        for python_cmd in python_commands:
            try:
                result = subprocess.run(
                    [python_cmd, '--version'],
                    capture_output=True,
                    timeout=2,
                    text=True
                )
                
                if result.returncode == 0:
                    version = result.stdout.strip() or result.stderr.strip()
                    self._cache['python'] = True
                    logger.info(f"Python check: available ({python_cmd} - {version})")
                    return True
                    
            except FileNotFoundError:
                continue
            except Exception as e:
                logger.debug(f"Error testing {python_cmd}: {e}")
                continue
        
        logger.warning("Python check failed - no valid Python interpreter found")
        self._cache['python'] = False
        return False
    
    def check_file_ops_available(self, use_cache: bool = True) -> bool:
        if use_cache and 'file_ops' in self._cache:
            return self._cache['file_ops']
        
        try:
            with tempfile.NamedTemporaryFile(mode='w+b', delete=True) as tmp:
                test_data = b'test data'
                tmp.write(test_data)
                tmp.flush()
                
                tmp.seek(0)
                read_data = tmp.read()
                
                available = read_data == test_data
                self._cache['file_ops'] = available
                logger.info(f"File operations check: {'available' if available else 'unavailable'}")
                return available
                
        except PermissionError as e:
            logger.warning(f"File operations check failed - permission denied: {e}")
            self._cache['file_ops'] = False
            return False
        except Exception as e:
            logger.error(f"File operations check failed: {e}")
            self._cache['file_ops'] = False
            return False
    
    def check_window_mgmt_available(self, use_cache: bool = True) -> bool:
        if use_cache and 'window_mgmt' in self._cache:
            return self._cache['window_mgmt']
        
        try:
            if not self.platform_adapter:
                logger.warning("Window management check failed - no platform adapter loaded")
                self._cache['window_mgmt'] = False
                return False
            
            required_methods = ['activate_window', 'close_window', 'list_windows']
            available_methods = [
                method for method in required_methods 
                if hasattr(self.platform_adapter, method)
            ]
            
            available = len(available_methods) > 0
            self._cache['window_mgmt'] = available
            
            if available:
                logger.info(f"Window management check: {'available' if available else 'unavailable'} - supported methods: {', '.join(available_methods)}")
            else:
                logger.warning(f"Window management check failed - platform adapter missing required methods")
            
            return available
            
        except Exception as e:
            logger.error(f"Window management check failed: {e}")
            self._cache['window_mgmt'] = False
            return False
    
    def check_recording_available(self, use_cache: bool = True) -> bool:
        if use_cache and 'recording' in self._cache:
            return self._cache['recording']
        
        try:
            if not self.platform_adapter:
                logger.warning("Recording check failed - no platform adapter loaded")
                self._cache['recording'] = False
                return False
            
            available = (
                hasattr(self.platform_adapter, 'start_recording') and 
                hasattr(self.platform_adapter, 'stop_recording')
            )
            
            self._cache['recording'] = available
            logger.info(f"Recording check: {'available' if available else 'unavailable'}")
            return available
            
        except Exception as e:
            logger.error(f"Recording check failed: {e}")
            self._cache['recording'] = False
            return False
    
    def check_accessibility_available(self, use_cache: bool = True) -> bool:
        if use_cache and 'accessibility' in self._cache:
            return self._cache['accessibility']
        
        try:
            if not self.accessibility_helper:
                logger.warning("Accessibility check failed - no accessibility helper loaded")
                self._cache['accessibility'] = False
                return False
            
            available = self.accessibility_helper.is_available()
            self._cache['accessibility'] = available
            logger.info(f"Accessibility check: {'available' if available else 'unavailable'}")
            return available
            
        except Exception as e:
            logger.error(f"Accessibility check failed: {e}")
            self._cache['accessibility'] = False
            return False
    
    def check_platform_adapter_available(self, use_cache: bool = True) -> bool:
        if use_cache and 'platform_adapter' in self._cache:
            return self._cache['platform_adapter']
        
        available = self.platform_adapter is not None
        self._cache['platform_adapter'] = available
        logger.info(f"Platform adapter check: {'available' if available else 'unavailable'}")
        return available
    
    def check_all_features(self, use_cache: bool = True) -> Dict[str, bool]:
        logger.info(f"Checking all features (platform: {self.platform})")
        
        results = {
            'accessibility': self.check_accessibility_available(use_cache),
            'screenshot': self.check_screenshot_available(use_cache),
            'recording': self.check_recording_available(use_cache),
            'shell': self.check_shell_available(use_cache),
            'python': self.check_python_available(use_cache),
            'file_ops': self.check_file_ops_available(use_cache),
            'window_mgmt': self.check_window_mgmt_available(use_cache),
            'platform_adapter': self.check_platform_adapter_available(use_cache),
        }
        
        available_count = sum(1 for v in results.values() if v)
        total_count = len(results)
        logger.info(f"Feature check completed: {available_count}/{total_count} features available")
        
        return results
    
    def clear_cache(self):
        self._cache.clear()
        logger.debug("Feature check cache cleared")
    
    def get_feature_report(self) -> Dict[str, Any]:
        results = self.check_all_features()
        
        return {
            'platform': {
                'system': self.platform,
                'release': platform.release(),
                'version': platform.version(),
                'machine': platform.machine(),
                'processor': platform.processor(),
            },
            'features': results,
            'summary': {
                'total': len(results),
                'available': sum(1 for v in results.values() if v),
                'unavailable': sum(1 for v in results.values() if not v),
            }
        }