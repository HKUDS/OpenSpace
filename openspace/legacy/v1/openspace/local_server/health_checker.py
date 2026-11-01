import requests
import os
from pathlib import Path
from typing import Dict, Tuple, Optional
from openspace.utils.logging import Logger
from openspace.local_server.feature_checker import FeatureChecker

logger = Logger.get_logger(__name__)

from openspace.utils.display import colorize as _c


class HealthStatus:
    """Health status"""
    def __init__(self, feature_available: bool, endpoint_available: Optional[bool], 
                 endpoint_detail: str = ""):
        self.feature_available = feature_available
        self.endpoint_available = endpoint_available
        self.endpoint_detail = endpoint_detail
    
    @property
    def fully_available(self) -> bool:
        """Fully available: feature and endpoint are available"""
        return self.feature_available and (self.endpoint_available == True)
    
    def __str__(self):
        if not self.feature_available:
            return "Feature N/A"
        elif self.endpoint_available is None:
            return "Feature OK (endpoint not tested)"
        elif self.endpoint_available:
            return f"OK ({self.endpoint_detail})"
        else:
            return f"Endpoint failed: {self.endpoint_detail}"


class HealthChecker:
    """Health checker with functional testing"""
    
    def __init__(self, feature_checker: FeatureChecker, 
                 base_url: str = "http://127.0.0.1:5000",
                 auto_cleanup: bool = True,
                 test_output_dir: str = None):
        self.feature_checker = feature_checker
        self.base_url = base_url
        self.results = {}
        self.auto_cleanup = auto_cleanup
        
        # set the test output directory
        if test_output_dir:
            self.test_output_dir = Path(test_output_dir)
        else:
            current_dir = Path(__file__).parent
            self.test_output_dir = current_dir / "temp"
        
        # create the directory
        self.test_output_dir.mkdir(exist_ok=True)
        
        self.temp_files = []  # Track temporary files for cleanup
        
        logger.info(f"Health checker initialized. Test output: {self.test_output_dir}, Auto-cleanup: {auto_cleanup}")
    
    def _get_test_file_path(self, filename: str) -> str:
        """Get path for a test file"""
        filepath = str(self.test_output_dir / filename)
        self._register_temp_file(filepath)
        return filepath
    
    def _register_temp_file(self, filepath: str):
        """Register a temporary file for later cleanup"""
        if filepath and filepath not in self.temp_files:
            self.temp_files.append(filepath)
    
    def cleanup_temp_files(self):
        """Clean up all temporary test files"""
        if not self.auto_cleanup:
            logger.info(f"Auto-cleanup disabled. Test files kept in: {self.test_output_dir}")
            return
        
        cleaned = 0
        for filepath in self.temp_files:
            try:
                if os.path.exists(filepath):
                    os.remove(filepath)
                    cleaned += 1
                    logger.debug(f"Cleaned up: {filepath}")
            except Exception as e:
                logger.warning(f"Failed to clean up {filepath}: {e}")
        
        self.temp_files.clear()
        
        # if the directory is empty, delete it
        try:
            if self.test_output_dir.exists() and not any(self.test_output_dir.iterdir()):
                self.test_output_dir.rmdir()
                logger.debug(f"Removed empty directory: {self.test_output_dir}")
        except:
            pass
        
        if cleaned > 0:
            logger.info(f"Cleaned up {cleaned} test files")
    
    def check_screenshot(self) -> Tuple[bool, str]:
        """Functionally test screenshot - actually take a screenshot and verify"""
        # 1. Check feature first
        if not self.feature_checker.check_screenshot_available():
            return False, "Feature N/A"
        
        # 2. Save screenshot to test directory
        screenshot_path = self._get_test_file_path("test_screenshot.png")
        
        try:
            response = requests.get(f"{self.base_url}/screenshot", timeout=10)
            
            if response.status_code != 200:
                return False, f"HTTP {response.status_code}"
            
            # 3. Save to file
            with open(screenshot_path, 'wb') as f:
                f.write(response.content)
            
            # 4. Verify it's actually an image
            content_type = response.headers.get('Content-Type', '')
            if 'image' not in content_type:
                return False, f"Invalid content type: {content_type}"
            
            # 5. Check file size (should be > 1KB)
            size_kb = len(response.content) / 1024
            if size_kb < 1:
                return False, "Image too small"
            
            logger.info(f"Screenshot saved: {screenshot_path} ({size_kb:.1f}KB)")
            return True, f"OK ({size_kb:.1f}KB)"
            
        except requests.exceptions.Timeout:
            return False, "Timeout"
        except Exception as e:
            return False, f"Error: {str(e)[:30]}"
    
    def check_cursor_position(self) -> Tuple[bool, str]:
        """Test cursor position"""
        if not self.feature_checker.check_screenshot_available():
            return False, "Feature N/A"
        
        try:
            response = requests.get(f"{self.base_url}/cursor_position", timeout=5)
            if response.status_code != 200:
                return False, f"HTTP {response.status_code}"
            
            data = response.json()
            if 'x' in data and 'y' in data:
                return True, f"({data['x']}, {data['y']})"
            return False, "Invalid response"
        except Exception as e:
            return False, str(e)[:30]
    
    def check_screen_size(self) -> Tuple[bool, str]:
        """Test screen size"""
        if not self.feature_checker.check_screenshot_available():
            return False, "Feature N/A"
        
        try:
            response = requests.get(f"{self.base_url}/screen_size", timeout=5)
            if response.status_code != 200:
                return False, f"HTTP {response.status_code}"
            
            data = response.json()
            if 'width' in data and 'height' in data:
                return True, f"{data['width']}x{data['height']}"
            return False, "Invalid response"
        except Exception as e:
            return False, str(e)[:30]
    
    def check_shell_command(self) -> Tuple[bool, str]:
        """Functionally test shell command execution"""
        if not self.feature_checker.check_shell_available():
            return False, "Feature N/A"
        
        try:
            response = requests.post(
                f"{self.base_url}/execute",
                json={"command": "echo hello_test", "shell": True},
                timeout=5
            )
            
            if response.status_code != 200:
                return False, f"HTTP {response.status_code}"
            
            data = response.json()
            output = data.get('output', '').strip()
            
            # Verify the command actually executed
            if 'hello_test' in output:
                return True, "Command executed"
            return False, "Command failed"
            
        except Exception as e:
            return False, str(e)[:30]
    
    def check_python_execution(self) -> Tuple[bool, str]:
        """Functionally test Python code execution"""
        if not self.feature_checker.check_python_available():
            return False, "Feature N/A"
        
        try:
            test_code = 'print("test_output_123")'
            response = requests.post(
                f"{self.base_url}/run_python",
                json={"code": test_code},
                timeout=5
            )
            
            if response.status_code != 200:
                return False, f"HTTP {response.status_code}"
            
            data = response.json()
            content = data.get('content', '')
            
            # Verify Python executed correctly
            if 'test_output_123' in content:
                return True, "Python executed"
            return False, "Execution failed"
            
        except Exception as e:
            return False, str(e)[:30]
    
    def check_bash_script(self) -> Tuple[bool, str]:
        """Functionally test Bash script execution"""
        if not self.feature_checker.check_shell_available():
            return False, "Feature N/A"
        
        try:
            response = requests.post(
                f"{self.base_url}/run_bash_script",
                json={"script": "echo bash_test_456"},
                timeout=5
            )
            
            if response.status_code != 200:
                return False, f"HTTP {response.status_code}"
            
            data = response.json()
            output = data.get('output', '')
            
            if 'bash_test_456' in output:
                return True, "Bash executed"
            return False, "Execution failed"
            
        except Exception as e:
            return False, str(e)[:30]
    
    def check_file_operations(self) -> Tuple[bool, str]:
        """Test file operations"""
        if not self.feature_checker.check_file_ops_available():
            return False, "Feature N/A"
        
        try:
            # Test list directory
            response = requests.post(
                f"{self.base_url}/list_directory",
                json={"path": "."},
                timeout=5
            )
            
            if response.status_code != 200:
                return False, f"HTTP {response.status_code}"
            
            data = response.json()
            if 'items' in data and isinstance(data['items'], list):
                return True, f"{len(data['items'])} items"
            return False, "Invalid response"
            
        except Exception as e:
            return False, str(e)[:30]
    
    def check_desktop_path(self) -> Tuple[bool, str]:
        """Test desktop path"""
        if not self.feature_checker.check_file_ops_available():
            return False, "Feature N/A"
        
        try:
            response = requests.get(f"{self.base_url}/desktop_path", timeout=5)
            if response.status_code != 200:
                return False, f"HTTP {response.status_code}"
            
            data = response.json()
            path = data.get('path', '')
            if path and os.path.exists(path):
                return True, "Path valid"
            return False, "Path not found"
        except Exception as e:
            return False, str(e)[:30]
    
    def check_window_management(self) -> Tuple[bool, str]:
        """Test window management"""
        if not self.feature_checker.check_window_mgmt_available():
            return False, "Feature N/A"
        
        try:
            # Just test if endpoint responds (window may not exist)
            response = requests.post(
                f"{self.base_url}/setup/activate_window",
                json={"window_name": "NonExistentWindow"},
                timeout=5
            )
            
            # 200 (success), 404 (not found), 501 (not supported) are all acceptable
            if response.status_code in [200, 404, 501]:
                return True, f"API available"
            return False, f"HTTP {response.status_code}"
        except Exception as e:
            return False, str(e)[:30]
    
    def check_recording(self) -> Tuple[bool, str]:
        """Functionally test recording - actually start and stop recording"""
        if not self.feature_checker.check_recording_available():
            return False, "Feature N/A"
        
        recording_path = self._get_test_file_path("test_recording.mp4")
        
        try:
            # 1. Start recording
            response = requests.post(f"{self.base_url}/start_recording", json={}, timeout=10)
            
            if response.status_code == 501:
                return False, "Not supported"
            
            if response.status_code != 200:
                return False, f"Start failed: {response.status_code}"
            
            # 2. Wait a bit
            import time
            time.sleep(3.0)  # Record for 3 seconds
            
            # 3. Stop recording
            response = requests.post(f"{self.base_url}/end_recording", json={}, timeout=15)
            
            if response.status_code == 200:
                # Save the recording file
                with open(recording_path, 'wb') as f:
                    f.write(response.content)
                
                size_kb = len(response.content) / 1024
                logger.info(f"Recording saved: {recording_path} ({size_kb:.1f}KB)")
                return True, f"OK ({size_kb:.1f}KB)"
            else:
                return False, f"Stop failed: {response.status_code}"
                
        except Exception as e:
            # Try to stop recording in case of error
            try:
                requests.post(f"{self.base_url}/end_recording", json={}, timeout=5)
            except:
                pass
            return False, str(e)[:30]
    
    def check_accessibility(self) -> Tuple[bool, str]:
        """Test accessibility tree"""
        if not self.feature_checker.check_accessibility_available():
            return False, "Feature N/A"
        
        try:
            response = requests.get(f"{self.base_url}/accessibility?max_depth=1", timeout=10)
            
            if response.status_code != 200:
                return False, f"HTTP {response.status_code}"
            
            data = response.json()
            if 'error' in data:
                return False, "Permission denied"
            
            # Should have some tree structure
            if 'platform' in data or 'children' in data:
                return True, "Tree available"
            return False, "Invalid response"
            
        except Exception as e:
            return False, str(e)[:30]
    
    def check_health_endpoint(self) -> Tuple[bool, str]:
        """Test health check endpoint"""
        try:
            response = requests.get(f"{self.base_url}/", timeout=5)
            if response.status_code == 200:
                data = response.json()
                if data.get('status') == 'ok':
                    return True, "OK"
            return False, f"HTTP {response.status_code}"
        except Exception as e:
            return False, str(e)[:30]
    
    def check_platform_info(self) -> Tuple[bool, str]:
        """Test platform info endpoint"""
        try:
            response = requests.get(f"{self.base_url}/platform", timeout=5)
            if response.status_code == 200:
                data = response.json()
                if 'system' in data:
                    return True, data['system']
            return False, f"HTTP {response.status_code}"
        except Exception as e:
            return False, str(e)[:30]
    
    def check_all(self, test_endpoints: bool = True) -> Dict[str, HealthStatus]:
        """
        Check all features with functional testing
        
        Args:
            test_endpoints: Whether to test endpoints (False only checks features)
        
        Returns:
            {Feature name: HealthStatus}
        """
        results = {}
        
        if not test_endpoints:
            # Only check features, not endpoints
            feature_results = self.feature_checker.check_all_features()
            for name, available in feature_results.items():
                results[name] = HealthStatus(available, None, "")
            self.results = results
            return results
        
        # Functional tests
        test_functions = {
            'Health Check': self.check_health_endpoint,
            'Platform Info': self.check_platform_info,
            'Screenshot': self.check_screenshot,
            'Cursor Position': self.check_cursor_position,
            'Screen Size': self.check_screen_size,
            'Shell Command': self.check_shell_command,
            'Python Execution': self.check_python_execution,
            'Bash Script': self.check_bash_script,
            'File Operations': self.check_file_operations,
            'Desktop Path': self.check_desktop_path,
            'Window Management': self.check_window_management,
            'Recording': self.check_recording,
            'Accessibility': self.check_accessibility,
        }
        
        for name, test_func in test_functions.items():
            success, detail = test_func()
            
            # Determine feature availability
            if detail == "Feature N/A":
                feature_available = False
                endpoint_available = None
            else:
                feature_available = True
                endpoint_available = success
            
            results[name] = HealthStatus(feature_available, endpoint_available, detail)
        
        # Clean up temporary files
        self.cleanup_temp_files()
        
        self.results = results
        return results
    
    def print_results(self, results: Dict[str, HealthStatus] = None, 
                     show_endpoint_details: bool = False):
        """Print check results"""
        if results is None:
            results = self.results
        
        if not results:
            return
        
        total = len(results)
        feature_available = sum(1 for s in results.values() if s.feature_available)
        fully_available = sum(1 for s in results.values() if s.fully_available)
        
        # Categorize
        basic = ['Health Check', 'Platform Info']
        
        # Basic Features
        print()
        print(_c("  - Basic", 'c', bold=True))
        basic_items = []
        for name in basic:
            if name in results:
                status = results[name]
                # Use colored dot instead of emoji
                if status.fully_available:
                    icon = _c("●", 'g')
                elif not status.feature_available:
                    icon = _c("●", 'rd')
                elif status.endpoint_available is None:
                    icon = _c("●", 'y')
                else:
                    icon = _c("●", 'y')
                
                text = _c(name, 'gr' if not status.feature_available else '')
                basic_items.append((icon, text, status))
        
        # Display in rows of 4
        for i in range(0, len(basic_items), 4):
            line_items = []
            for j in range(4):
                if i + j < len(basic_items):
                    icon, text, status = basic_items[i + j]
                    line_items.append(f"{icon} {text:<15}")
            print("     " + " ".join(line_items))
        
        # Show details if requested
        if show_endpoint_details:
            for name in basic:
                if name in results:
                    status = results[name]
                    print(f"       {_c('·', 'gr')} {name}: {_c(str(status), 'gr')}")
        
        # Advanced Features
        print()
        print(_c("  - Advanced", 'c', bold=True))
        advanced_items = []
        for name, status in results.items():
            if name not in basic:
                # Use colored dot instead of emoji
                if status.fully_available:
                    icon = _c("●", 'g')
                elif not status.feature_available:
                    icon = _c("●", 'rd')
                elif status.endpoint_available is None:
                    icon = _c("●", 'y')
                else:
                    icon = _c("●", 'y')
                
                text = _c(name, 'gr' if not status.feature_available else '')
                advanced_items.append((icon, text, status))
        
        # Display in rows of 4
        for i in range(0, len(advanced_items), 4):
            line_items = []
            for j in range(4):
                if i + j < len(advanced_items):
                    icon, text, _ = advanced_items[i + j]
                    line_items.append(f"{icon} {text:<15}")
            print("     " + " ".join(line_items))
        
        # Show details if requested
        if show_endpoint_details:
            for name, status in results.items():
                if name not in basic:
                    print(f"       {_c('·', 'gr')} {name}: {_c(str(status), 'gr')}")
        
        # Summary
        from openspace.utils.display import print_separator
        print()
        print_separator()
        print(f"  {_c('Summary:', 'c', bold=True)} {_c(str(feature_available) + '/' + str(total), 'g' if feature_available == total else 'y')} features available", end='')
        if any(s.endpoint_available is not None for s in results.values()):
            print(f", {_c(str(fully_available) + '/' + str(total), 'g' if fully_available == total else 'y')} fully functional")
        else:
            print()
        print_separator()
        
        # Legend
        print(f"  {_c('Legend:', 'gr')} {_c('●', 'g')} Available  {_c('●', 'y')} Partial/Untested  {_c('●', 'rd')} Unavailable")
        
        # Test files info
        if self.temp_files and not self.auto_cleanup:
            print()
            print(f"  {_c('Test files saved:', 'y')} {self.test_output_dir}")
            print(f"  {_c(str(len(self.temp_files)) + ' file(s) available for inspection', 'gr')}")
        
        print()
    
    def get_summary(self) -> dict:
        """Get summary"""
        if not self.results:
            return {}
        
        total = len(self.results)
        feature_available = sum(1 for s in self.results.values() if s.feature_available)
        fully_available = sum(1 for s in self.results.values() if s.fully_available)
        
        return {
            'total': total,
            'feature_available': feature_available,
            'fully_available': fully_available,
            'details': {k: str(v) for k, v in self.results.items()}
        }
    
    def get_simple_features_dict(self) -> Dict[str, bool]:
        """Get simple feature dict (for banner display)"""
        return self.feature_checker.check_all_features()